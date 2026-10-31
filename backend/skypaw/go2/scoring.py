"""Time-to-rescue scoring: detection -> Go2 arrival, per victim and for the mission.

Times are mission seconds (PROTOCOL ``t``: seconds since mission start). For each victim:

* ``time_to_rescue_s`` = arrival - detection (the headline metric);
* ``travel_s`` = arrival - dispatch (the Go2's share);
* ``wait_s`` = dispatch - detection (decision latency: confirm, queueing).

Mission totals: victims detected / reached, sum, mean and max time-to-rescue over the
reached victims, and the ids still unreached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VictimScore:
    """Timeline of one victim (``None`` = not happened yet)."""

    victim_id: str
    detected_t: float | None = None
    dispatched_t: float | None = None
    arrived_t: float | None = None
    distance_error_m: float | None = None
    status: str = "located"  # located | enroute | arrived | failed

    @property
    def time_to_rescue_s(self) -> float | None:
        """Arrival minus detection."""
        if self.detected_t is None or self.arrived_t is None:
            return None
        return self.arrived_t - self.detected_t

    @property
    def travel_s(self) -> float | None:
        """Arrival minus dispatch."""
        if self.dispatched_t is None or self.arrived_t is None:
            return None
        return self.arrived_t - self.dispatched_t

    @property
    def wait_s(self) -> float | None:
        """Dispatch minus detection."""
        if self.detected_t is None or self.dispatched_t is None:
            return None
        return self.dispatched_t - self.detected_t

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form (rounded to 0.01 s / 1 mm)."""
        return {
            "victim_id": self.victim_id,
            "status": self.status,
            "detected_t": _r(self.detected_t),
            "dispatched_t": _r(self.dispatched_t),
            "arrived_t": _r(self.arrived_t),
            "wait_s": _r(self.wait_s),
            "travel_s": _r(self.travel_s),
            "time_to_rescue_s": _r(self.time_to_rescue_s),
            "distance_error_m": _r(self.distance_error_m, 3),
        }


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


@dataclass
class RescueScore:
    """Score book for one mission."""

    mission_id: str = ""
    victims: dict[str, VictimScore] = field(default_factory=dict)

    def _get(self, victim_id: str) -> VictimScore:
        return self.victims.setdefault(victim_id, VictimScore(victim_id))

    def detected(self, victim_id: str, t: float) -> None:
        """Record the first detection time (later detections do not move it)."""
        v = self._get(victim_id)
        if v.detected_t is None:
            v.detected_t = float(t)

    def dispatched(self, victim_id: str, t: float) -> None:
        """Record the Go2 dispatch (the latest dispatch wins, e.g. after a re-route)."""
        v = self._get(victim_id)
        v.dispatched_t = float(t)
        v.status = "enroute"

    def arrived(self, victim_id: str, t: float, distance_error_m: float | None = None) -> None:
        """Record the first Go2 arrival at the victim."""
        v = self._get(victim_id)
        if v.arrived_t is None:
            v.arrived_t = float(t)
            v.distance_error_m = distance_error_m
        v.status = "arrived"

    def failed(self, victim_id: str) -> None:
        """Mark a dispatch that ended without arrival."""
        v = self._get(victim_id)
        if v.arrived_t is None:
            v.status = "failed"

    def totals(self) -> dict[str, Any]:
        """Mission totals over all victims."""
        ttr = [v.time_to_rescue_s for v in self.victims.values() if v.time_to_rescue_s is not None]
        detected = [v for v in self.victims.values() if v.detected_t is not None]
        return {
            "victims": len(self.victims),
            "detected": len(detected),
            "reached": len(ttr),
            "unreached": sorted(v.victim_id for v in detected if v.arrived_t is None),
            "time_to_rescue_total_s": _r(sum(ttr)) if ttr else None,
            "time_to_rescue_mean_s": _r(sum(ttr) / len(ttr)) if ttr else None,
            "time_to_rescue_max_s": _r(max(ttr)) if ttr else None,
        }

    def as_dict(self) -> dict[str, Any]:
        """Per-victim rows (sorted by id) and totals."""
        return {
            "mission_id": self.mission_id,
            "victims": [self.victims[k].as_dict() for k in sorted(self.victims)],
            "totals": self.totals(),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """The score as JSON text."""
        return json.dumps(self.as_dict(), indent=indent)


__all__ = ["RescueScore", "VictimScore"]
