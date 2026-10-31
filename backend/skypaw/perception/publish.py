"""Publish candidates to Cyberwave: one environment waypoint and at most one alert each.

* Waypoints: name ``cand-<id>``, world-frame position, collection ``skypaw-candidates``,
  metadata ``{victim_id, fused, life_sign, suggested_triage, sensors, frames, source}``
  (RESEARCH 1.5). Idempotent by name: an identical waypoint is left alone; a moved one gets
  ``update_waypoint(position=...)``; changed metadata (which the PATCH cannot change) is a
  delete + re-create of that one waypoint. Missing ones are created in one bulk POST.
* Alerts (RESEARCH 1.7, SDK ``twin.alerts``): ``critical`` for a life sign, ``warning`` for
  the decoy pattern (suggested triage ``expectant``). Deduplicated by alert name against the
  twin's active/acknowledged alerts; metadata links the waypoint (name and id).

Every synthetic reading inside ``sensors`` keeps its ``source: "synthetic"`` label.
"""

from __future__ import annotations

import math
from typing import Any

from skypaw.logging import get_logger, log_event
from skypaw.mission.ports import Candidate
from skypaw.sim.client import assert_client_simulation

log = get_logger(__name__)

COLLECTION = "skypaw-candidates"
ALERT_TYPE = "skypaw_candidate"
POS_TOL_M = 0.01


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def waypoint_name(victim_id: str) -> str:
    return f"cand-{victim_id}"


def waypoint_metadata(c: Candidate) -> dict[str, Any]:
    """The waypoint metadata schema (PROTOCOL / RESEARCH 1.5)."""
    ev = c.evidence
    return {
        "kind": "candidate",
        "victim_id": c.victim_id,
        "fused": c.fused,
        "life_sign": c.life_sign,
        "suggested_triage": c.suggested_triage,
        "sensors": {
            "rgb": ev.get("rgb"),
            "thermal": ev.get("sensors", {}).get("thermal"),
            "radar": ev.get("sensors", {}).get("radar"),
            "acoustic": ev.get("acoustic"),
            "co2": ev.get("co2"),
        },
        "frames": list(c.frames),
        "source": dict(c.source),
    }


def waypoint_spec(c: Candidate) -> dict[str, Any]:
    x, y, z = (float(v) for v in c.position)
    return {
        "name": waypoint_name(c.victim_id),
        "collection": COLLECTION,
        "position": {"x": x, "y": y, "z": z},
        "metadata": waypoint_metadata(c),
    }


def _pos(obj: Any) -> tuple[float, float, float] | None:
    p = _get(obj, "position")
    if p is None:
        return None
    return (float(_get(p, "x", 0.0)), float(_get(p, "y", 0.0)), float(_get(p, "z", 0.0)))


def publish_waypoints(cw: Any, env_id: str, cands: list[Candidate]) -> dict[str, dict[str, Any]]:
    """Create/update ``cand-<id>`` waypoints; returns ``{name: {action, id}}``."""
    assert_client_simulation(cw)
    existing = {str(_get(w, "name")): w for w in cw.environments.get_waypoints(env_id)}
    result: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    for c in cands:
        spec = waypoint_spec(c)
        name = spec["name"]
        have = existing.get(name)
        if have is None:
            missing.append(spec)
            continue
        wid = str(_get(have, "id"))
        if (_get(have, "metadata") or {}) != spec["metadata"]:
            cw.environments.delete_waypoint(env_id, wid)
            missing.append(spec)
            result[name] = {"action": "replace"}
            continue
        hp = _pos(have)
        want = (spec["position"]["x"], spec["position"]["y"], spec["position"]["z"])
        if hp is None or math.dist(hp, want) > POS_TOL_M:
            cw.environments.update_waypoint(env_id, wid, position=spec["position"])
            result[name] = {"action": "move", "id": wid}
        else:
            result[name] = {"action": "skip", "id": wid}
    if missing:
        after = cw.environments.create_waypoints(env_id, missing)
        ids = {str(_get(w, "name")): str(_get(w, "id")) for w in after}
        for spec in missing:
            prev = result.get(spec["name"], {}).get("action")
            result[spec["name"]] = {"action": prev or "create", "id": ids.get(spec["name"])}
    log_event(
        log,
        "candidates_waypoints",
        env_id=env_id,
        **{a: sum(1 for r in result.values() if r["action"] == a) for a in ("create", "skip")},
    )
    return result


def alert_for(c: Candidate) -> dict[str, Any] | None:
    """Alert payload for a candidate, or ``None`` if it does not warrant one."""
    if c.life_sign:
        severity, what = "critical", "life sign"
    elif c.suggested_triage == "expectant":
        severity, what = "warning", "possible decoy (thermal only, radar negative)"
    else:
        return None
    return {
        "name": f"{waypoint_name(c.victim_id)}: {what}",
        "severity": severity,
        "description": (
            f"{c.victim_id} fused {c.fused:.2f}, suggested {c.suggested_triage}; "
            "thermal/radar readings are synthetic (SPEC 3.2)"
        ),
    }


def publish_alerts(
    twin: Any, env_id: str, cands: list[Candidate], waypoints: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Raise one alert per life-sign / decoy candidate; dedupe by name. Returns actions."""
    assert_client_simulation(twin.client)
    active = {a.name for a in twin.alerts.list(status="active,acknowledged")}
    out = []
    for c in cands:
        spec = alert_for(c)
        if spec is None:
            continue
        if spec["name"] in active:
            out.append({"name": spec["name"], "action": "skip"})
            continue
        wp = waypoints.get(waypoint_name(c.victim_id), {})
        alert = twin.alerts.create(
            spec["name"],
            description=spec["description"],
            alert_type=ALERT_TYPE,
            severity=spec["severity"],
            source_type="simulation",
            environment_uuid=env_id,
            metadata={
                "victim_id": c.victim_id,
                "waypoint_name": waypoint_name(c.victim_id),
                "waypoint_id": wp.get("id"),
                "fused": c.fused,
                "suggested_triage": c.suggested_triage,
                "life_sign": c.life_sign,
                "source": dict(c.source),
            },
        )
        active.add(spec["name"])
        out.append({"name": spec["name"], "action": "create", "uuid": str(alert.uuid)})
    log_event(log, "candidates_alerts", created=sum(1 for a in out if a["action"] == "create"))
    return out


__all__ = [
    "ALERT_TYPE",
    "COLLECTION",
    "alert_for",
    "publish_alerts",
    "publish_waypoints",
    "waypoint_metadata",
    "waypoint_name",
    "waypoint_spec",
]
