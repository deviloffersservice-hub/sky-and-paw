"""Synthetic sensor models (SPEC 3.2): thermal, UWB life-detection radar, acoustic, CO2.

Cyberwave renders RGB (and depth) only, so these sensors read the **ground-truth victim
placements from the scene config** (``site.yaml`` ``victims``, published by the sim
engineer) and add seeded noise. Every reading carries ``"source": "synthetic"``; nothing here
is ever presented as a real measurement.

Behaviour (SPEC 6.3 and the approved mockup engine ``docs/mockups/ops-v2.html``):

* ``thermal(pos)``: Delta-T (deg C vs ambient) of the truth nearest ``pos`` within
  ``assoc_radius_m`` that has a thermal signature, plus N(0, ``thermal_sigma``); else
  ``None``. The decoy is the sun-heated sheet metal (Delta-T 9.2, "too hot for a body").
* ``thermal_hotspots(footprint)``: the synthetic thermal *camera*: truth heat sources inside
  the camera's ground footprint, positions with N(0, ``hotspot_sigma_m``) noise. This is how
  a non-visual target (the decoy, or a victim fully under a slab) becomes a candidate.
* ``uwb_radar(pos)``: fires only when the sensor is within ``radar_radius_m`` (2.6 m, SPEC
  6.3) horizontally of ``pos``. A truth with a radar signature gives ``{bpm, snr}`` (bpm +
  N(0, ``bpm_sigma``), rounded); a truth without one, or no truth at all, gives a
  **negative** reading ``{negative: True}``.
* ``acoustic(pos, after_speech=...)``: tapping (``True``) only after the Go2 has spoken, if
  the truth has an acoustic response; hook for M4.
* ``co2(pos)``: void concentration in ppm when the Go2 is on site (hook for M4).

Noise is deterministic: each reading draws from ``numpy.random.default_rng([seed, crc32(key)])``
where the key names the truth and the sensor, so the same seed gives the same value for the
same target regardless of call order.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from skypaw.scene.site import Site, load_site, site_to_world

SYNTHETIC = "synthetic"
#: SPEC 6.3: detection fires when the drone passes within 2.6 m of a ground-truth person.
DETECTION_RADIUS_M = 2.6


@dataclass(frozen=True)
class Truth:
    """One ground-truth placement from ``site.yaml``, world frame."""

    id: str
    world: tuple[float, float, float]
    decoy: bool
    rgb: float | None
    thermal: float | None
    radar: dict[str, Any] | None
    co2: float | None
    acoustic: bool | None
    language: str | None
    where: str | None

    def dist_xy(self, pos: Sequence[float]) -> float:
        return math.hypot(self.world[0] - float(pos[0]), self.world[1] - float(pos[1]))


def load_truth(site: Site | None = None) -> list[Truth]:
    """Ground truth from the scene config (``site.yaml`` ``victims``), positions in world."""
    site = site or load_site()
    out = []
    for v in site.victims:
        t = v.get("truth") or {}
        x, y, z = (float(c) for c in v["pos"])
        radar = t.get("radar")
        out.append(
            Truth(
                id=str(v["id"]),
                world=site_to_world(x, y, z),
                decoy=bool(v.get("decoy", False)),
                rgb=float(t["rgb"]) if t.get("rgb") is not None else None,
                thermal=float(t["thermal"]) if t.get("thermal") is not None else None,
                radar=dict(radar) if radar else None,
                co2=float(t["co2"]) if t.get("co2") is not None else None,
                acoustic=bool(t["acoustic"]) if t.get("acoustic") is not None else None,
                language=v.get("language"),
                where=v.get("where"),
            )
        )
    return out


def _point_in_polygon(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i][0], poly[i][1]
        x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


class SyntheticSensors:
    """All synthetic sensors over one set of truths; deterministic for a given seed."""

    def __init__(
        self,
        truths: Sequence[Truth] | None = None,
        *,
        seed: int = 0,
        assoc_radius_m: float = 1.5,
        radar_radius_m: float = DETECTION_RADIUS_M,
        thermal_sigma: float = 0.2,
        bpm_sigma: float = 2.0,
        hotspot_sigma_m: float = 0.15,
        co2_sigma_ppm: float = 10.0,
    ) -> None:
        self.truths = list(truths) if truths is not None else load_truth()
        self.seed = int(seed)
        self.assoc_radius_m = float(assoc_radius_m)
        self.radar_radius_m = float(radar_radius_m)
        self.thermal_sigma = float(thermal_sigma)
        self.bpm_sigma = float(bpm_sigma)
        self.hotspot_sigma_m = float(hotspot_sigma_m)
        self.co2_sigma_ppm = float(co2_sigma_ppm)

    # -- helpers -----------------------------------------------------------------------
    def rng(self, key: str) -> np.random.Generator:
        return np.random.default_rng([self.seed, zlib.crc32(key.encode("utf-8"))])

    def nearest(self, pos: Sequence[float], radius: float | None = None) -> Truth | None:
        """Nearest truth within ``radius`` (default ``assoc_radius_m``) horizontally."""
        radius = self.assoc_radius_m if radius is None else radius
        best = min(self.truths, key=lambda t: t.dist_xy(pos), default=None)
        if best is None or best.dist_xy(pos) > radius:
            return None
        return best

    # -- sensors -------------------------------------------------------------------------
    def thermal(self, pos: Sequence[float]) -> dict[str, Any] | None:
        """``{delta_t, source, truth_id}`` or ``None`` (no heat source near ``pos``)."""
        t = self.nearest(pos)
        if t is None or t.thermal is None:
            return None
        dt = t.thermal + float(self.rng(f"{t.id}:thermal").normal(0.0, self.thermal_sigma))
        return {"delta_t": round(dt, 2), "source": SYNTHETIC, "truth_id": t.id}

    def thermal_hotspots(self, footprint: Sequence[Sequence[float]]) -> list[dict[str, Any]]:
        """Heat sources inside the camera ground footprint (polygon of world (X, Y))."""
        out = []
        if len(footprint) < 3:
            return out
        for t in self.truths:
            if t.thermal is None or not _point_in_polygon(t.world[0], t.world[1], footprint):
                continue
            dx, dy = self.rng(f"{t.id}:hotspot").normal(0.0, self.hotspot_sigma_m, size=2)
            out.append(
                {
                    "position": [round(t.world[0] + dx, 3), round(t.world[1] + dy, 3), t.world[2]],
                    "source": SYNTHETIC,
                    "truth_id": t.id,
                }
            )
        return out

    def uwb_radar(
        self, pos: Sequence[float], sensor_pos: Sequence[float] | None = None
    ) -> dict[str, Any] | None:
        """Radar reading for a target at ``pos`` from a sensor at ``sensor_pos``.

        ``None`` = out of range (no reading). Otherwise ``{bpm, snr, negative, source}``;
        ``negative=True`` means scanned with no life signs (decoy pattern).
        """
        if sensor_pos is not None:
            d = math.hypot(float(pos[0]) - sensor_pos[0], float(pos[1]) - sensor_pos[1])
            if d > self.radar_radius_m:
                return None
        t = self.nearest(pos)
        if t is None or not t.radar:
            return {
                "bpm": None,
                "snr": None,
                "negative": True,
                "source": SYNTHETIC,
                "truth_id": t.id if t else None,
            }
        bpm = float(t.radar["bpm"]) + float(self.rng(f"{t.id}:radar").normal(0.0, self.bpm_sigma))
        return {
            "bpm": int(round(bpm)),
            "snr": str(t.radar["snr"]),
            "negative": False,
            "source": SYNTHETIC,
            "truth_id": t.id,
        }

    def acoustic(self, pos: Sequence[float], *, after_speech: bool = False) -> dict | None:
        """Tapping heard after the Go2 speaks (hook for M4; mockup: any non-decoy responds
        unless its truth says ``acoustic: false``)."""
        t = self.nearest(pos)
        if not after_speech or t is None or t.decoy or t.acoustic is False:
            return None
        return {"tapping": True, "source": SYNTHETIC, "truth_id": t.id}

    def co2(self, pos: Sequence[float]) -> dict[str, Any] | None:
        """Void CO2 concentration in ppm at the Go2 (hook for M4)."""
        t = self.nearest(pos)
        if t is None or t.co2 is None:
            return None
        ppm = t.co2 + float(self.rng(f"{t.id}:co2").normal(0.0, self.co2_sigma_ppm))
        return {"ppm": round(ppm, 1), "source": SYNTHETIC, "truth_id": t.id}


__all__ = [
    "DETECTION_RADIUS_M",
    "SYNTHETIC",
    "SyntheticSensors",
    "Truth",
    "load_truth",
]
