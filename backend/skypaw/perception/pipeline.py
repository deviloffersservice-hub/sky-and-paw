"""``DetectionPipeline``: the stream B implementation of ``ports.Detector``.

``on_frame(frame)`` -> RGB detect (YOLO, else colour markers) -> project box centres to the
world (``project.pixel_to_world`` on the plane ``Z = plane_z``) -> add the synthetic thermal
camera's hotspots inside the frame footprint -> cluster into the running candidate set
(1.0 m) -> attach synthetic thermal / UWB radar readings -> fuse (SPEC 6.3) -> the updated
``Candidate`` list.

Ids are stable: a candidate takes the id of the nearest ``site.yaml`` truth within
``id_radius_m`` (1.5 m; one truth per candidate, the closer candidate wins), else
``C-01``, ``C-02``, ... in first-seen order. The detector itself never sees truth positions;
only the synthetic sensors and the id assignment read the scene config.

Sensor rules (see ``skypaw.sensors.synthetic``): thermal is an imaging sensor, so a candidate
seen in a frame gets a thermal reading; the UWB radar only reads when a frame's drone
position was within 2.6 m (SPEC 6.3) horizontally of the candidate, and a negative scan sets
``radar_negative``. Readings are deterministic per truth (seeded), so they are recomputed
from the candidate state each time instead of being accumulated.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np

from skypaw.mission.ports import Candidate, Frame
from skypaw.perception.cluster import MERGE_RADIUS_M, Cluster, Hit, add_hits
from skypaw.perception.detector import CompositeDetector, DetectionResult
from skypaw.perception.fusion import fuse
from skypaw.perception.project import (
    MOUNT_DOWN,
    CameraPose,
    ground_footprint,
    pixel_to_world,
    position_xyz,
)
from skypaw.scene.markers import MARKER_NOTE
from skypaw.sensors.synthetic import SYNTHETIC, SyntheticSensors

#: Plane used to project RGB boxes: victims in site.yaml lie 0.4-0.6 m above the ground
#: (on or in the rubble), the marker box tops are 0.6-0.8 m. No depth on the X2.
TARGET_Z = 0.5
ID_RADIUS_M = 1.5


@dataclass
class FrameRecord:
    """What one frame contributed (for detections.json and latency reporting)."""

    frame_id: str
    fired: str
    boxes: list[dict[str, Any]]
    hits: list[dict[str, Any]]
    footprint: list[list[float]]
    detect_ms: float
    total_ms: float
    drone_position: list[float]


@dataclass
class _Track:
    cluster: Cluster
    seen_from: list[list[float]] = field(default_factory=list)  # drone positions


class DetectionPipeline:
    """Running candidate set over frames; implements ``ports.Detector``."""

    def __init__(
        self,
        detector: CompositeDetector,
        sensors: SyntheticSensors,
        *,
        plane_z: float = TARGET_Z,
        merge_radius_m: float = MERGE_RADIUS_M,
        id_radius_m: float = ID_RADIUS_M,
        mount: np.ndarray = MOUNT_DOWN,
    ) -> None:
        self.detector = detector
        self.sensors = sensors
        self.plane_z = float(plane_z)
        self.merge_radius_m = float(merge_radius_m)
        self.id_radius_m = float(id_radius_m)
        self.mount = mount
        self.clusters: list[Cluster] = []
        self.drone_positions: list[list[float]] = []
        self.records: list[FrameRecord] = []

    # -- ports.Detector ----------------------------------------------------------------
    async def on_frame(self, frame: Frame) -> list[Candidate]:
        return await asyncio.to_thread(self.process, frame)

    # -- work --------------------------------------------------------------------------
    def process(self, frame: Frame, image: np.ndarray | None = None) -> list[Candidate]:
        t0 = time.perf_counter()
        if image is None:
            image = cv2.imread(frame.path, cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"cannot read frame image {frame.path}")
        res: DetectionResult = self.detector.run(image)
        pose = CameraPose.from_frame(frame.position, frame.rotation, frame.intrinsics, self.mount)
        hits: list[Hit] = []
        for b in res.boxes:
            p = pixel_to_world(pose, *b.centre, plane_z=self.plane_z)
            if p is None:
                continue
            hits.append(
                Hit(
                    p,
                    "person" if b.person else "object",
                    b.conf if b.person else None,
                    b.detector,
                    frame.frame_id,
                    b.label,
                )
            )
        footprint = ground_footprint(pose, 0.0)
        for hs in self.sensors.thermal_hotspots(footprint):
            hits.append(Hit(tuple(hs["position"]), "thermal", None, SYNTHETIC, frame.frame_id))
        add_hits(self.clusters, hits, self.merge_radius_m)
        drone = position_xyz(frame.position)
        self.drone_positions.append(drone)
        cands = self.candidates()
        self.records.append(
            FrameRecord(
                frame_id=frame.frame_id,
                fired=res.fired,
                boxes=[b.as_dict() for b in res.boxes],
                hits=[
                    {
                        "position": [round(v, 3) for v in h.position],
                        "kind": h.kind,
                        "conf": h.conf,
                        "detector": h.detector,
                        "label": h.label,
                    }
                    for h in hits
                ],
                footprint=[[round(x, 3), round(y, 3)] for x, y in footprint],
                detect_ms=res.latency_ms,
                total_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                drone_position=[round(v, 3) for v in drone],
            )
        )
        return cands

    def _assign_ids(self) -> list[str]:
        truths = self.sensors.truths
        ids: list[str | None] = [None] * len(self.clusters)
        pairs = []
        for i, c in enumerate(self.clusters):
            for t in truths:
                d = c.dist_xy(t.world)
                if d <= self.id_radius_m:
                    pairs.append((d, i, t.id))
        used: set[str] = set()
        for _d, i, tid in sorted(pairs):
            if ids[i] is None and tid not in used:
                ids[i] = tid
                used.add(tid)
        n = 0
        out = []
        for v in ids:
            if v is None:
                n += 1
                v = f"C-{n:02d}"
            out.append(v)
        return out

    def _radar(self, pos: tuple[float, float, float]) -> dict[str, Any] | None:
        """Best radar reading over every drone position so far (positive beats negative)."""
        best = None
        for dp in self.drone_positions:
            r = self.sensors.uwb_radar(pos, sensor_pos=dp)
            if r is None:
                continue
            if not r["negative"]:
                return r
            best = r
        return best

    def candidates(self) -> list[Candidate]:
        out = []
        for vid, c in zip(self._assign_ids(), self.clusters, strict=True):
            pos = c.position
            thermal = self.sensors.thermal(pos)
            radar = self._radar(pos)
            best = c.best_person
            evidence: dict[str, Any] = {
                "rgb": round(best.conf, 3) if best and best.conf is not None else None,
                "thermal": thermal["delta_t"] if thermal else None,
                "radar": (
                    {"bpm": radar["bpm"], "snr": radar["snr"], "source": SYNTHETIC}
                    if radar and not radar["negative"]
                    else None
                ),
                "radar_negative": bool(radar and radar["negative"]),
                "acoustic": None,
                "co2": None,
                "objects": sorted({h.label for h in c.hits if h.kind == "object"}),
                "sensors": {"thermal": thermal, "radar": radar},
            }
            f = fuse(evidence)
            source: dict[str, str] = {
                "rgb": best.detector if best else "none",
                "thermal": SYNTHETIC,
                "radar": SYNTHETIC,
                "position": "rgb-projection" if c.visual else "synthetic-thermal",
            }
            if any(h.detector == "marker" for h in c.hits):
                source["rgb_note"] = MARKER_NOTE
            out.append(
                Candidate(
                    victim_id=vid,
                    position=[round(v, 3) for v in pos],
                    evidence=evidence,
                    fused=round(f.fused, 4),
                    life_sign=f.life_sign,
                    suggested_triage=f.suggested_triage,
                    frames=c.frames,
                    source=source,
                )
            )
        return out

    def report(self) -> dict[str, Any]:
        """JSON-friendly state: candidates, per-frame records, latency summary."""
        cands = self.candidates()
        totals = [r.total_ms for r in self.records]
        return {
            "candidates": [asdict(c) for c in cands],
            "frames": [asdict(r) for r in self.records],
            "latency_ms": {
                "frames": len(totals),
                "mean": round(sum(totals) / len(totals), 1) if totals else None,
                "max": max(totals) if totals else None,
            },
        }


def truth_errors(cands: list[Candidate], sensors: SyntheticSensors) -> dict[str, float | None]:
    """Horizontal distance from each candidate to the truth with the same id (None if none)."""
    by_id = {t.id: t for t in sensors.truths}
    out: dict[str, float | None] = {}
    for c in cands:
        t = by_id.get(c.victim_id)
        out[c.victim_id] = (
            round(math.hypot(c.position[0] - t.world[0], c.position[1] - t.world[1]), 3)
            if t
            else None
        )
    return out


__all__ = ["ID_RADIUS_M", "TARGET_Z", "DetectionPipeline", "FrameRecord", "truth_errors"]
