"""Camera frames from simulated twins over REST (``latest-frame``), with ``None`` handling.

RESEARCH §6 / day one: ``twin.camera.get_frame("numpy", sensor_id=...)`` returns ``None``
while the run is loading, then a uniform placeholder (solid blue, std ~0), then real
640x360 BGR renders. :func:`is_usable` rejects black and uniform frames.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from skypaw.logging import get_logger, log_event
from skypaw.sim.client import assert_client_simulation

log = get_logger(__name__)

#: Acceptance thresholds (M1-A4): mean pixel value > 5; a frame with std < 1 is a placeholder.
MIN_MEAN = 5.0
MIN_STD = 1.0


def capture(twin: Any, sensor_id: str, fmt: str = "numpy") -> Any | None:
    """One frame from ``twin``'s ``sensor_id``, or ``None`` while the simulator is not ready.

    Uses ``twin.camera.get_frame`` (never ``twin.camera[sensor]``, which opens MQTT).
    SDK "simulation not running / not ready" errors are reported as ``None``.
    """
    assert_client_simulation(twin.client)
    try:
        return twin.camera.get_frame(fmt, sensor_id=sensor_id)
    except Exception as exc:  # noqa: BLE001 - SDK raises several not-ready types
        name = type(exc).__name__
        if name in ("SimulationNotRunningError", "SimulationLevelError"):
            log_event(log, "frame_not_ready", level=10, sensor_id=sensor_id, error=name)
            return None
        raise


@dataclass(frozen=True)
class FrameStats:
    """Basic statistics of a frame."""

    shape: tuple[int, ...]
    mean: float
    std: float

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {"shape": list(self.shape), "mean": self.mean, "std": self.std}


def frame_stats(frame: np.ndarray) -> FrameStats:
    """Shape, mean and standard deviation of a frame."""
    arr = np.asarray(frame)
    return FrameStats(tuple(arr.shape), round(float(arr.mean()), 2), round(float(arr.std()), 2))


def is_placeholder(frame: np.ndarray, *, min_std: float = MIN_STD) -> bool:
    """The loading placeholder: a uniform, clearly blue frame (day one: mean 85, std ~0).

    A uniform *grey* frame is a real render (the X2 bottom camera at 0.6 m sees only the
    flat ground plane: mean ~69-119, std < 1), so uniformity alone is not rejected.
    """
    arr = np.asarray(frame, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return False
    if max(float(arr[..., c].std()) for c in range(3)) >= min_std:
        return False
    b, g, r = (float(arr[..., c].mean()) for c in range(3))  # BGR
    return b - max(g, r) > 20.0


def is_usable(frame: Any, *, min_mean: float = MIN_MEAN, min_std: float = MIN_STD) -> bool:
    """True for a real render: an image array that is not black and not the placeholder."""
    if not isinstance(frame, np.ndarray) or frame.ndim < 2 or frame.size == 0:
        return False
    stats = frame_stats(frame)
    return stats.mean > min_mean and not is_placeholder(frame, min_std=min_std)


@dataclass
class FrameResult:
    """Outcome of :func:`wait_for_frame`."""

    sensor_id: str
    frame: np.ndarray | None
    elapsed_s: float
    attempts: int
    stats: FrameStats | None

    @property
    def ok(self) -> bool:
        """A usable frame was captured."""
        return self.frame is not None


def wait_for_frame(
    twin: Any,
    sensor_id: str,
    *,
    timeout_s: float = 40.0,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FrameResult:
    """Poll :func:`capture` until :func:`is_usable` or ``timeout_s``; logs the outcome."""
    t0 = clock()
    attempts = 0
    last: FrameStats | None = None
    while True:
        attempts += 1
        frame = capture(twin, sensor_id)
        if isinstance(frame, np.ndarray):
            last = frame_stats(frame)
            if is_usable(frame):
                result = FrameResult(sensor_id, frame, round(clock() - t0, 2), attempts, last)
                log_event(
                    log,
                    "frame_ok",
                    sensor_id=sensor_id,
                    elapsed_s=result.elapsed_s,
                    attempts=attempts,
                    **last.as_dict(),
                )
                return result
        if clock() - t0 >= timeout_s:
            log_event(
                log,
                "frame_timeout",
                level=30,
                sensor_id=sensor_id,
                attempts=attempts,
                last=last.as_dict() if last else None,
            )
            return FrameResult(sensor_id, None, round(clock() - t0, 2), attempts, last)
        sleep(poll_s)


def save_jpeg(frame: np.ndarray, path: Path | str, *, quality: int = 90) -> Path:
    """Write a BGR frame (as returned by ``get_frame("numpy")``) to ``path`` as JPEG."""
    import cv2

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", np.asarray(frame), [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError(f"could not encode frame of shape {np.asarray(frame).shape} as JPEG")
    path.write_bytes(buf.tobytes())
    return path


__all__ = [
    "MIN_MEAN",
    "MIN_STD",
    "FrameResult",
    "FrameStats",
    "capture",
    "frame_stats",
    "is_placeholder",
    "is_usable",
    "save_jpeg",
    "wait_for_frame",
]
