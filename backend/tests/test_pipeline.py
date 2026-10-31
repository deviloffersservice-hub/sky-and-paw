"""End-to-end pipeline on real captured frames (tests/fixtures/frames).

1. The real M2 overhead frame (rubble only, no people, no markers): no candidates.
2. The same real frame with marker box tops painted where a drone at four survey poses
   would see them (the live scene has no humans; markers stand in, see scene/markers.py):
   exactly the four site.yaml targets, within 0.5 m, with the SPEC triage
   (V-01 immediate, V-02 delayed, V-03 unknown, D-01 expectant) - M3-A4 offline.
3. Live V1-B frames, when present: every candidate within 1.0 m of its truth.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import cv2
import pytest
from perception_helpers import FIXTURES, paint_markers

from skypaw.mission.ports import Frame
from skypaw.perception.detector import CompositeDetector
from skypaw.perception.pipeline import DetectionPipeline, truth_errors
from skypaw.perception.project import CameraPose, yaw_to_quat_wxyz
from skypaw.scene.markers import MARKER_NOTE
from skypaw.sensors.synthetic import SyntheticSensors

M2 = FIXTURES / "frames" / "m2_overhead_bottom.jpg"
# Drone poses (world x, y, z, yaw): each sees one or two targets; all within 2.6 m of one.
POSES = {
    "p_v01": (2.5, 0.5, 6.0, 0.0),
    "p_d01": (-6.0, 4.0, 6.0, 0.0),
    "p_v03": (6.0, -3.5, 6.0, 0.0),
    "p_v02": (-4.5, -2.0, 6.0, 0.3),
}
EXPECTED = {"V-01": "immediate", "V-02": "delayed", "V-03": "unknown", "D-01": "expectant"}


def _pipeline(seed: int = 0) -> DetectionPipeline:
    return DetectionPipeline(CompositeDetector(None), SyntheticSensors(seed=seed))


def _painted_frames(tmp_path: Path, sensors: SyntheticSensors) -> list[Frame]:
    frames = []
    for i, (name, (x, y, z, yaw)) in enumerate(POSES.items()):
        rot = yaw_to_quat_wxyz(yaw)
        img = cv2.imread(str(M2))
        painted = paint_markers(img, CameraPose.from_frame([x, y, z], rot), sensors.truths)
        assert painted, name
        path = tmp_path / f"{name}.jpg"
        cv2.imwrite(str(path), img)
        frames.append(Frame(name, str(path), float(i), "camera_chasis_bottom", [x, y, z], rot))
    return frames


def test_real_rubble_frame_gives_no_candidates() -> None:
    meta = json.loads(M2.with_suffix(".json").read_text())
    pipe = _pipeline()
    frame = Frame("m2", str(M2), 0.0, "camera_chasis_bottom", meta["position"], meta["rotation"])
    assert pipe.process(frame) == []
    assert pipe.records[0].fired == "none"


def test_painted_markers_on_real_frame_end_to_end(tmp_path: Path) -> None:
    pipe = _pipeline()
    cands = []
    for f in _painted_frames(tmp_path, pipe.sensors):
        cands = asyncio.run(pipe.on_frame(f))
    by_id = {c.victim_id: c for c in cands}
    assert set(by_id) == set(EXPECTED)  # no other candidates
    errs = truth_errors(cands, pipe.sensors)
    assert all(e is not None and e < 0.5 for e in errs.values()), errs
    assert {k: c.suggested_triage for k, c in by_id.items()} == EXPECTED
    v01, d01 = by_id["V-01"], by_id["D-01"]
    assert v01.fused >= 0.99 and v01.life_sign
    assert v01.source["rgb"] == "marker" and v01.source["rgb_note"] == MARKER_NOTE
    assert v01.source["thermal"] == "synthetic" and v01.evidence["radar"]["source"] == "synthetic"
    assert d01.evidence["rgb"] is None and d01.evidence["radar_negative"]
    assert d01.evidence["objects"] == ["decoy_marker"]
    assert by_id["V-03"].evidence["thermal"] is None and by_id["V-03"].evidence["rgb"] > 0.5
    assert by_id["V-02"].evidence["radar"]["snr"] == "weak"
    assert all(r.fired == "marker" for r in pipe.records)


def test_pipeline_is_deterministic_for_a_seed(tmp_path: Path) -> None:
    def run(seed: int) -> list:
        pipe = _pipeline(seed)
        out = []
        for f in _painted_frames(tmp_path, pipe.sensors):
            out = pipe.process(f)
        return [(c.victim_id, c.fused, c.evidence["thermal"]) for c in out]

    assert run(3) == run(3)


def test_unknown_object_far_from_truth_gets_c_id(tmp_path: Path) -> None:
    from skypaw.sensors.synthetic import Truth

    pipe = _pipeline()
    fake = Truth("X", (0.5, 0.5, 0.5), False, None, None, None, None, None, None, None)
    img = cv2.imread(str(M2))
    rot = yaw_to_quat_wxyz(0.0)
    paint_markers(img, CameraPose.from_frame([0.0, 0.0, 6.0], rot), [fake])
    path = tmp_path / "x.jpg"
    cv2.imwrite(str(path), img)
    cands = pipe.process(Frame("x", str(path), 0.0, "cam", [0.0, 0.0, 6.0], rot))
    assert [c.victim_id for c in cands] == ["C-01"]
    assert cands[0].evidence["radar_negative"] and cands[0].suggested_triage == "unknown"


LIVE = sorted((FIXTURES / "frames").glob("v1b_*.jpg"))


@pytest.mark.skipif(not LIVE, reason="no live V1-B frames committed")
def test_live_v1b_frames_project_markers_near_truth() -> None:
    pipe = _pipeline()
    cands = []
    for p in LIVE:
        meta = json.loads(p.with_suffix(".json").read_text())
        f = Frame(p.stem, str(p), 0.0, meta["sensor_id"], meta["position"], meta["rotation"])
        cands = pipe.process(f)
    assert cands, "live frame shows markers but nothing was detected"
    for c in cands:
        t = next(t for t in pipe.sensors.truths if t.id == c.victim_id)
        assert math.hypot(c.position[0] - t.world[0], c.position[1] - t.world[1]) <= 1.0
