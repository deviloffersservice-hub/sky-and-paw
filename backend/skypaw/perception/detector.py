"""RGB detectors: YOLOv8n person detector, a colour-marker detector, and a composite.

* :class:`YoloDetector`: ``ultralytics`` YOLOv8n (pinned in ``requirements-ml.txt``), COCO
  class 0 ``person`` only, ``conf >= 0.5``, CPU. Weights live under
  ``DATA_DIR/models/yolov8n.pt``; they are downloaded once from the pinned URL and refused
  unless their SHA-256 matches :data:`YOLO_SHA256`. This is the production path.
* :class:`MarkerDetector`: HSV colour blobs for the live-scene markers of
  ``skypaw.scene.markers`` (the MuJoCo scene has no humans). Orange boxes are **person
  stand-ins** (``person=True``); slate-grey boxes are the decoy prop and are reported as
  non-person objects (``person=False``). Its confidence is a pseudo-confidence (below).
* :class:`CompositeDetector`: YOLO first; if YOLO finds no person (or is not installed),
  the marker detector's boxes are used. ``last_fired`` / ``DetectionResult.fired`` is
  ``"yolo"`` or ``"marker"`` (``"none"`` when nothing was found) and becomes
  ``Candidate.source["rgb"]``.

Images are BGR ``numpy`` arrays (``camera.get_frame("numpy")``, ``cv2.imread``).
"""

from __future__ import annotations

import hashlib
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from skypaw.logging import get_logger, log_event

log = get_logger(__name__)

YOLO_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
YOLO_FILENAME = "yolov8n.pt"
#: SHA-256 of yolov8n.pt from the ultralytics v8.3.0 release (6 549 796 bytes).
YOLO_SHA256 = "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36"
PERSON_CLASS = 0
MIN_CONF = 0.5


@dataclass(frozen=True)
class Box:
    """One detection in pixel coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float
    label: str  # "person" | "victim_marker" | "decoy_marker"
    detector: str  # "yolo" | "marker"
    person: bool = True

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "conf": round(self.conf, 3),
            "label": self.label,
            "detector": self.detector,
            "person": self.person,
        }


@dataclass
class DetectionResult:
    """Boxes for one image plus which detector produced them and how long it took."""

    boxes: list[Box] = field(default_factory=list)
    fired: str = "none"  # "yolo" | "marker" | "none"
    latency_ms: float = 0.0
    yolo_ms: float | None = None
    marker_ms: float | None = None


class RgbDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[Box]: ...


# --- YOLO ---------------------------------------------------------------------------------
class WeightsError(RuntimeError):
    """Weights missing, not downloadable or with the wrong hash."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_weights(models_dir: Path, *, download: bool = True) -> Path:
    """Path of verified ``yolov8n.pt`` under ``models_dir``; downloads once if allowed."""
    models_dir = Path(models_dir)
    path = models_dir / YOLO_FILENAME
    if not path.exists():
        if not download:
            raise WeightsError(f"{path} is missing and download is disabled")
        models_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        log_event(log, "yolo_weights_download", url=YOLO_URL, dest=str(path))
        with urllib.request.urlopen(YOLO_URL, timeout=120) as resp:  # noqa: S310 - pinned https
            tmp.write_bytes(resp.read())
        tmp.replace(path)
    digest = _sha256_file(path)
    if digest != YOLO_SHA256:
        raise WeightsError(f"{path} sha256 {digest} != pinned {YOLO_SHA256}")
    return path


def yolo_available() -> bool:
    """True if ``ultralytics`` imports (``make setup-ml``)."""
    try:
        import ultralytics  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class YoloDetector:
    """YOLOv8n on CPU, person class only, ``conf >= min_conf``."""

    def __init__(
        self,
        models_dir: Path | str,
        *,
        min_conf: float = MIN_CONF,
        imgsz: int = 640,
        download: bool = True,
    ) -> None:
        from ultralytics import YOLO

        self.weights = ensure_weights(Path(models_dir), download=download)
        self.model = YOLO(str(self.weights))
        self.min_conf = float(min_conf)
        self.imgsz = int(imgsz)

    def detect(self, image: np.ndarray) -> list[Box]:
        res = self.model.predict(
            image,
            conf=self.min_conf,
            classes=[PERSON_CLASS],
            device="cpu",
            imgsz=self.imgsz,
            verbose=False,
        )[0]
        boxes = []
        for xyxy, conf, cls in zip(
            res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist(), strict=True
        ):
            if int(cls) != PERSON_CLASS or conf < self.min_conf:
                continue
            boxes.append(Box(*(float(v) for v in xyxy), float(conf), "person", "yolo", True))
        return boxes


# --- colour markers -------------------------------------------------------------------------
@dataclass(frozen=True)
class HsvRange:
    """Inclusive OpenCV HSV range (H 0..179, S and V 0..255)."""

    lo: tuple[int, int, int]
    hi: tuple[int, int, int]


#: Bright orange (RGB 255,110,0 -> H ~13) under the MuJoCo lighting.
ORANGE = HsvRange((5, 140, 90), (24, 255, 255))
#: Slate grey (RGB 96,112,140 -> H ~109, S ~80). Plain rubble grey has S ~0 and is excluded.
SLATE = HsvRange((98, 45, 50), (122, 150, 230))


class MarkerDetector:
    """HSV blob detector for the marker boxes (stand-ins for people; see scene/markers.py).

    Pseudo-confidence for a blob: ``0.5 + 0.45 * solidity * min(1, area / full_area_px)``,
    where ``full_area_px`` is the expected pixel area of an unoccluded 0.6 m box top seen
    from about 6 m (35 x 35 px at f = 320 px). A full, compact blob scores ~0.95; small or
    ragged (partly occluded) blobs score towards 0.5. Blobs under ``min_area_px`` are noise.

    **Shape fallback** (``shape_fallback=True``, used only when no colour blob is found):
    the live V1-B runs showed that Cyberwave's MuJoCo import drops the GLB material colour,
    so the markers render rubble-grey. The fallback finds bright (grey > ``bright_level``),
    compact, square components whose sides are those of a 0.6 m box top at ~5-7 m range
    (``shape_side_px``, 24-50 px at f = 320 px); every rubble block in the real frames is
    >= 55 px and the slabs are far larger. Such squares are labelled ``marker_shape`` and
    treated as person stand-ins (the decoy prop is a flat 1.2 x 0.8 m sheet, which fails the
    square test). Pseudo-confidence: ``0.5 + 0.45 * fill * min(1, area / full_area_px)``.
    Squares touching the image border are skipped (their size is unknown).
    """

    def __init__(
        self,
        *,
        min_area_px: float = 60.0,
        full_area_px: float = 1200.0,
        victim: HsvRange = ORANGE,
        decoy: HsvRange = SLATE,
        shape_fallback: bool = True,
        bright_level: int = 160,
        shape_side_px: tuple[float, float] = (24.0, 50.0),
        shape_max_aspect: float = 1.3,
        shape_min_fill: float = 0.85,
    ) -> None:
        self.min_area_px = float(min_area_px)
        self.full_area_px = float(full_area_px)
        self.shape_fallback = bool(shape_fallback)
        self.bright_level = int(bright_level)
        self.shape_side_px = (float(shape_side_px[0]), float(shape_side_px[1]))
        self.shape_max_aspect = float(shape_max_aspect)
        self.shape_min_fill = float(shape_min_fill)
        self.ranges = {"victim_marker": victim, "decoy_marker": decoy}
        self.kernel = np.ones((3, 3), np.uint8)

    def mask(self, image: np.ndarray, label: str) -> np.ndarray:
        rng = self.ranges[label]
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        m = cv2.inRange(hsv, np.array(rng.lo, np.uint8), np.array(rng.hi, np.uint8))
        return cv2.morphologyEx(m, cv2.MORPH_OPEN, self.kernel)

    def detect(self, image: np.ndarray) -> list[Box]:
        boxes: list[Box] = []
        for label in self.ranges:
            contours, _ = cv2.findContours(
                self.mask(image, label), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            for c in contours:
                area = float(cv2.contourArea(c))
                if area < self.min_area_px:
                    continue
                hull = float(cv2.contourArea(cv2.convexHull(c))) or area
                solidity = area / hull
                conf = 0.5 + 0.45 * solidity * min(1.0, area / self.full_area_px)
                x, y, w, h = cv2.boundingRect(c)
                boxes.append(
                    Box(
                        float(x),
                        float(y),
                        float(x + w),
                        float(y + h),
                        round(min(0.95, conf), 3),
                        label,
                        "marker",
                        person=label == "victim_marker",
                    )
                )
        if not boxes and self.shape_fallback:
            boxes = self.detect_shapes(image)
        boxes.sort(key=lambda b: -b.conf)
        return boxes

    def detect_shapes(self, image: np.ndarray) -> list[Box]:
        """Grey 0.6 m marker squares by size and shape (see class docstring)."""
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        m = ((grey > self.bright_level) * 255).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.kernel)
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        lo, hi = self.shape_side_px
        img_h, img_w = grey.shape[:2]
        boxes = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < lo * lo * self.shape_min_fill:
                continue
            (_cx, _cy), (w, h), _ang = cv2.minAreaRect(c)
            if not (lo <= min(w, h) and max(w, h) <= hi):
                continue
            if max(w, h) / max(min(w, h), 1.0) > self.shape_max_aspect:
                continue
            fill = area / max(w * h, 1.0)
            if fill < self.shape_min_fill:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if x <= 1 or y <= 1 or x + bw >= img_w - 1 or y + bh >= img_h - 1:
                continue
            conf = 0.5 + 0.45 * min(1.0, fill) * min(1.0, area / self.full_area_px)
            boxes.append(
                Box(
                    float(x),
                    float(y),
                    float(x + bw),
                    float(y + bh),
                    round(min(0.95, conf), 3),
                    "marker_shape",
                    "marker",
                    person=True,
                )
            )
        return boxes


# --- composite ------------------------------------------------------------------------------
class CompositeDetector:
    """YOLO first, colour markers as the fallback; records which one fired."""

    def __init__(self, yolo: RgbDetector | None, marker: RgbDetector | None = None) -> None:
        self.yolo = yolo
        self.marker = marker if marker is not None else MarkerDetector()
        self.last_fired = "none"

    @classmethod
    def from_settings(cls, data_dir: Path | str, *, use_yolo: bool = True) -> CompositeDetector:
        """YOLO from ``DATA_DIR/models`` when ultralytics is installed, else markers only."""
        yolo = None
        if use_yolo and yolo_available():
            try:
                yolo = YoloDetector(Path(data_dir) / "models")
            except Exception as exc:  # noqa: BLE001 - fall back to markers, but say so
                log_event(log, "yolo_unavailable", level=30, error=f"{type(exc).__name__}: {exc}")
        return cls(yolo)

    def run(self, image: np.ndarray) -> DetectionResult:
        t0 = time.perf_counter()
        out = DetectionResult()
        if self.yolo is not None:
            ty = time.perf_counter()
            people = [b for b in self.yolo.detect(image) if b.person]
            out.yolo_ms = round((time.perf_counter() - ty) * 1000.0, 1)
            if people:
                out.boxes, out.fired = people, "yolo"
        if not out.boxes:
            tm = time.perf_counter()
            boxes = self.marker.detect(image)
            out.marker_ms = round((time.perf_counter() - tm) * 1000.0, 1)
            if boxes:
                out.boxes, out.fired = boxes, "marker"
        out.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        self.last_fired = out.fired
        return out

    def detect(self, image: np.ndarray) -> list[Box]:
        return self.run(image).boxes


__all__ = [
    "MIN_CONF",
    "ORANGE",
    "SLATE",
    "YOLO_SHA256",
    "YOLO_URL",
    "Box",
    "CompositeDetector",
    "DetectionResult",
    "HsvRange",
    "MarkerDetector",
    "WeightsError",
    "YoloDetector",
    "ensure_weights",
    "yolo_available",
]
