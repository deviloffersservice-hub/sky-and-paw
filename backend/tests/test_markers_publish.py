"""Victim markers (scene/markers.py) and candidate publishing (perception/publish.py), offline."""

from __future__ import annotations

import json
import struct
from types import SimpleNamespace as NS
from typing import Any

import pytest
from test_scene import FakeCyberwave

from skypaw.mission.ports import Candidate
from skypaw.perception.publish import (
    publish_alerts,
    publish_waypoints,
    waypoint_metadata,
)
from skypaw.scene.markers import (
    ASSET_NAMES,
    MARKER_NOTE,
    build_marker_glb,
    ensure_markers,
    marker_specs,
)
from skypaw.scene.site import load_site


def _gltf_json(glb: bytes) -> dict[str, Any]:
    n = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20 : 20 + n])


def test_marker_glb_is_deterministic_and_coloured() -> None:
    a, b = build_marker_glb("victim"), build_marker_glb("victim")
    assert a == b and len(a) < 5000
    victim = _gltf_json(a)["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"]
    decoy = _gltf_json(build_marker_glb("decoy"))["materials"][0]["pbrMetallicRoughness"]
    assert victim[0] == 1.0 and victim[2] == 0.0  # orange
    assert decoy["baseColorFactor"][2] > decoy["baseColorFactor"][0]  # slate (bluish grey)
    acc = _gltf_json(a)["accessors"]
    pos = next(x for x in acc if x.get("type") == "VEC3" and "max" in x)
    assert pos["max"] == pytest.approx([0.3, 0.3, 0.2]) and pos["min"] == pytest.approx(
        [-0.3, -0.3, -0.2]
    )


def test_marker_specs_come_from_site_yaml() -> None:
    specs = {s.name: s for s in marker_specs(load_site())}
    assert set(specs) == {"marker-V-01", "marker-V-02", "marker-V-03", "marker-D-01"}
    assert specs["marker-V-01"].world == (3.5, 2.0, 0.5)
    assert specs["marker-D-01"].kind == "decoy" and specs["marker-V-02"].kind == "victim"


def _env(cw: FakeCyberwave) -> str:
    e = NS(uuid="env-1", name="skypaw-v1-dev-b", project_uuid="p")
    cw.envs.append(e)
    return e.uuid


def test_ensure_markers_creates_named_twins_then_is_idempotent() -> None:
    cw = FakeCyberwave()
    env = _env(cw)
    out = ensure_markers(cw, env)
    assert cw.writes.count("POST asset") == 2 and cw.writes.count("POST twin") == 4
    twins = {t.name: t for t in cw.twins_db}
    assert len({t.uuid for t in twins.values()}) == 4  # one twin per victim, no reuse
    assert twins["marker-V-02"].position_x == -4.0 and twins["marker-V-02"].position_y == -3.0
    assert all(t.fixed_base is True for t in twins.values())
    assert {a.name for a in cw.assets_db} == set(ASSET_NAMES.values())
    assert all(a.fixed_base and "mujoco" in a.supported_simulation_backends for a in cw.assets_db)
    cw.writes.clear()
    again = ensure_markers(cw, env)
    assert cw.writes == []
    assert again["twins"] == out["twins"]


def _cand(vid: str, *, life: bool, triage: str, fused: float = 0.9) -> Candidate:
    return Candidate(
        victim_id=vid,
        position=[1.0, 2.0, 0.5],
        evidence={
            "rgb": 0.9 if life else None,
            "thermal": 5.8,
            "radar": {"bpm": 64, "snr": "strong", "source": "synthetic"} if life else None,
            "radar_negative": not life,
            "acoustic": None,
            "co2": None,
            "sensors": {"thermal": {"delta_t": 5.8, "source": "synthetic"}, "radar": None},
        },
        fused=fused,
        life_sign=life,
        suggested_triage=triage,
        frames=["f1"],
        source={
            "rgb": "marker",
            "thermal": "synthetic",
            "radar": "synthetic",
            "rgb_note": MARKER_NOTE,
        },
    )


class FakeAlerts:
    def __init__(self, cw: Any) -> None:
        self.db: list[Any] = []
        self.cw = cw

    def list(self, status: str | None = None) -> list[Any]:
        return list(self.db)

    def create(self, name: str, **kw: Any) -> Any:
        self.cw.writes.append("POST alert")
        a = NS(uuid=f"al-{len(self.db)}", name=name, **kw)
        self.db.append(a)
        return a


def _publish_cw() -> tuple[FakeCyberwave, str, Any]:
    cw = FakeCyberwave()
    env = _env(cw)

    def delete_waypoint(e: str, wid: str) -> list:
        cw.writes.append("DELETE waypoint")
        cw.waypoints[e] = [w for w in cw.waypoints[e] if w.id != wid]
        return cw.waypoints[e]

    def create_waypoints(e: str, wps: list[dict]) -> list:
        cw.writes.append("POST waypoints")
        for w in wps:
            cw.waypoints.setdefault(e, []).append(
                NS(
                    id=f"wp-{w['name']}",
                    name=w["name"],
                    position=w["position"],
                    metadata=json.loads(json.dumps(w["metadata"])),
                )
            )
        return list(cw.waypoints[e])

    cw.environments.delete_waypoint = delete_waypoint  # type: ignore[attr-defined]
    cw.environments.create_waypoints = create_waypoints  # type: ignore[method-assign]
    twin = NS(client=cw, alerts=FakeAlerts(cw))
    return cw, env, twin


def test_publish_waypoints_and_alerts_idempotent() -> None:
    cw, env, twin = _publish_cw()
    cands = [
        _cand("V-01", life=True, triage="immediate"),
        _cand("D-01", life=False, triage="expectant", fused=0.7),
        _cand("V-03", life=False, triage="unknown", fused=0.64),
    ]
    wps = publish_waypoints(cw, env, cands)
    assert {k: v["action"] for k, v in wps.items()} == {
        "cand-V-01": "create",
        "cand-D-01": "create",
        "cand-V-03": "create",
    }
    stored = {w.name: w for w in cw.waypoints[env]}
    md = stored["cand-V-01"].metadata
    assert {"victim_id", "fused", "life_sign", "suggested_triage", "sensors"} <= set(md)
    assert md["sensors"]["thermal"]["source"] == "synthetic"
    alerts = publish_alerts(twin, env, cands, wps)
    sev = {a.name: a.severity for a in twin.alerts.db}
    assert sev == {
        "cand-V-01: life sign": "critical",
        "cand-D-01: possible decoy (thermal only, radar negative)": "warning",
    }
    assert twin.alerts.db[0].metadata["waypoint_id"] == "wp-cand-V-01"
    assert [a["action"] for a in alerts] == ["create", "create"]

    cw.writes.clear()
    wps2 = publish_waypoints(cw, env, cands)
    publish_alerts(twin, env, cands, wps2)
    assert cw.writes == []  # second run creates nothing
    assert all(v["action"] == "skip" for v in wps2.values())


def test_changed_metadata_replaces_only_that_waypoint() -> None:
    cw, env, _ = _publish_cw()
    c = _cand("V-01", life=True, triage="immediate")
    publish_waypoints(cw, env, [c])
    cw.writes.clear()
    c.fused = 0.999
    res = publish_waypoints(cw, env, [c])
    assert res["cand-V-01"]["action"] == "replace"
    assert cw.writes == ["DELETE waypoint", "POST waypoints"]
    assert waypoint_metadata(c)["fused"] == 0.999
