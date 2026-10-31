"""RGB detectors: colour markers (always), YOLOv8n on person photos (needs make setup-ml)."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from perception_helpers import FIXTURES, bgr, weights_dir

from skypaw.perception.detector import (
    YOLO_SHA256,
    CompositeDetector,
    MarkerDetector,
    WeightsError,
    ensure_weights,
    yolo_available,
)
from skypaw.scene.markers import DECOY_RGBA, VICTIM_RGBA

PEOPLE = sorted((FIXTURES / "people").glob("*.jpg"))
WEIGHTS = weights_dir()
needs_yolo = pytest.mark.skipif(
    not yolo_available() or WEIGHTS is None,
    reason="ultralytics (make setup-ml) and DATA_DIR/models/yolov8n.pt are required; "
    "unit tests never download",
)


def _box_image(rgba: tuple[int, int, int, int], *, size: int = 36) -> np.ndarray:
    img = np.full((360, 640, 3), 92, np.uint8)  # MuJoCo ground grey
    cv2.rectangle(img, (100, 60), (260, 200), (205, 205, 205), -1)  # rubble slab
    cv2.rectangle(img, (400, 150), (400 + size, 150 + size), bgr(rgba), -1)
    return img


def test_marker_detector_finds_orange_box_as_person_stand_in() -> None:
    boxes = MarkerDetector().detect(_box_image(VICTIM_RGBA))
    assert len(boxes) == 1
    b = boxes[0]
    assert b.label == "victim_marker" and b.person and b.detector == "marker"
    assert 0.9 <= b.conf <= 0.95
    assert b.centre == pytest.approx((418.0, 168.0), abs=1.5)


def test_marker_detector_reports_slate_decoy_as_non_person() -> None:
    boxes = MarkerDetector().detect(_box_image(DECOY_RGBA))
    assert [(b.label, b.person) for b in boxes] == [("decoy_marker", False)]


def test_small_partial_blob_has_lower_pseudo_confidence() -> None:
    small = MarkerDetector().detect(_box_image(VICTIM_RGBA, size=12))
    assert len(small) == 1 and 0.5 <= small[0].conf < 0.65


def test_marker_detector_has_no_false_positive_on_real_rubble_frame() -> None:
    img = cv2.imread(str(FIXTURES / "frames" / "m2_overhead_bottom.jpg"))
    assert MarkerDetector().detect(img) == []


def test_composite_falls_back_to_markers_and_records_it() -> None:
    class NoPeople:
        def detect(self, image: np.ndarray) -> list:
            return []

    comp = CompositeDetector(NoPeople())
    res = comp.run(_box_image(VICTIM_RGBA))
    assert res.fired == "marker" and comp.last_fired == "marker" and res.yolo_ms is not None
    assert CompositeDetector(None).run(np.full((360, 640, 3), 92, np.uint8)).fired == "none"


def test_weights_hash_is_enforced(tmp_path: pytest.TempPathFactory) -> None:
    models = tmp_path  # type: ignore[assignment]
    with pytest.raises(WeightsError):
        ensure_weights(models, download=False)  # missing, no download in tests
    (models / "yolov8n.pt").write_bytes(b"not the weights")
    with pytest.raises(WeightsError, match=YOLO_SHA256):
        ensure_weights(models, download=False)


@needs_yolo
def test_yolo_detects_people_in_fixture_photos() -> None:
    """>= 2 of the 3 committed public-domain person photos yield a person box >= 0.5."""
    from skypaw.perception.detector import YoloDetector

    det = YoloDetector(WEIGHTS, download=False)  # type: ignore[arg-type]
    assert len(PEOPLE) == 3
    hits = 0
    for p in PEOPLE:
        boxes = det.detect(cv2.imread(str(p)))
        assert all(b.conf >= 0.5 and b.label == "person" for b in boxes)
        hits += bool(boxes)
    assert hits >= 2


@needs_yolo
def test_yolo_latency_and_no_person_on_rubble() -> None:
    """M3-A3 budget: mean latency per 640x360 frame <= 400 ms on CPU (after warm-up)."""
    from skypaw.perception.detector import YoloDetector

    det = YoloDetector(WEIGHTS, download=False)  # type: ignore[arg-type]
    img = cv2.imread(str(FIXTURES / "frames" / "m2_overhead_bottom.jpg"))
    assert det.detect(img) == []
    t0 = time.perf_counter()
    for _ in range(5):
        det.detect(img)
    assert (time.perf_counter() - t0) / 5 * 1000 <= 400
