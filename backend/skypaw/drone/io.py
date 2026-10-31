"""What the survey executor needs from the world: pose, flight controls, a camera.

The SDK adapters wrap the calls verified live in the M2 probe (``scripts/overhead.py``,
``tests/evidence/M2/dev/overhead-flight.json``): ``twin.takeoff(altitude=6)`` over MQTT,
``twin.locomotion.turn_left/turn_right(rate, duration=)`` and
``twin.locomotion.move_forward(speed, duration=)`` bursts (20 Hz publish then ``stop``), and
``twin.camera.get_frame("numpy", sensor_id=...)``. Pose comes only from MQTT
(``skypaw.sim.mqtt_ws.PoseCache``). Every adapter call is blocking; the executor runs them
with ``asyncio.to_thread``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from skypaw.sim.client import assert_client_simulation
from skypaw.sim.frames import capture, is_usable
from skypaw.sim.mqtt_ws import PoseCache

#: Downward camera of the Skydio X2 (RESEARCH §6).
BOTTOM_CAMERA = "camera_chasis_bottom"
#: Catalog camera model (M2 task): 640x360, fov 58.72 deg, treated as MuJoCo ``fovy``.
CAMERA_W, CAMERA_H, CAMERA_FOVY_DEG = 640, 360, 58.72


def yaw_of(q: dict[str, float]) -> float:
    """Yaw (rad, about world +Z) of a quaternion ``{x, y, z, w}``."""
    x, y, z, w = (float(q.get(k, 0.0)) for k in "xyzw")
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def wrap(a: float) -> float:
    """Angle wrapped to (-pi, pi]."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def camera_intrinsics(
    w: int = CAMERA_W, h: int = CAMERA_H, fovy_deg: float = CAMERA_FOVY_DEG
) -> dict[str, Any]:
    """Pinhole intrinsics for the bottom camera (vertical fov, square pixels)."""
    fy = (h / 2) / math.tan(math.radians(fovy_deg) / 2)
    return {
        "width": w,
        "height": h,
        "fovy_deg": fovy_deg,
        "fx": round(fy, 3),
        "fy": round(fy, 3),
        "cx": w / 2,
        "cy": h / 2,
        "model": "pinhole",
        "note": "fov from the catalog, assumed vertical (MuJoCo fovy)",
    }


@dataclass(frozen=True)
class Pose:
    """World-frame pose; ``t`` is the epoch time the sample was received."""

    x: float
    y: float
    z: float
    yaw: float
    q: tuple[float, float, float, float]  # w, x, y, z
    t: float

    @property
    def xyz(self) -> list[float]:
        """``[x, y, z]`` rounded for the wire."""
        return [round(self.x, 3), round(self.y, 3), round(self.z, 3)]


class PoseSource(Protocol):
    """Latest pose of one twin."""

    def latest(self) -> Pose | None: ...


class FlightControls(Protocol):
    """Blocking flight commands (each returns when the burst is over)."""

    def takeoff(self, altitude: float) -> None: ...
    def turn(self, rate: float, duration: float) -> None: ...  # rate > 0 = counter-clockwise
    def forward(self, speed: float, duration: float) -> None: ...
    def stop(self) -> None: ...
    def land(self) -> None: ...


class FrameGrabber(Protocol):
    """One BGR frame from the downward camera, or ``None`` when not available."""

    def grab(self) -> np.ndarray | None: ...


def pose_from_cache_entry(entry: dict[str, Any] | None) -> Pose | None:
    """Build a :class:`Pose` from a ``PoseCache.latest`` entry (needs position)."""
    if not entry or "position" not in entry:
        return None
    p = entry["position"]
    q = entry.get("rotation") or {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0}
    t = float(entry.get("position_t") or entry.get("t") or 0.0)
    return Pose(
        x=float(p.get("x", 0.0)),
        y=float(p.get("y", 0.0)),
        z=float(p.get("z", 0.0)),
        yaw=yaw_of(q),
        q=(
            float(q.get("w", 1.0)),
            float(q.get("x", 0.0)),
            float(q.get("y", 0.0)),
            float(q.get("z", 0.0)),
        ),
        t=t,
    )


class CachePoseSource:
    """:class:`PoseSource` over the MQTT :class:`PoseCache` for one twin uuid."""

    def __init__(self, cache: PoseCache, twin_uuid: str) -> None:
        self.cache = cache
        self.twin_uuid = twin_uuid

    def latest(self) -> Pose | None:
        """Latest MQTT pose of the twin."""
        return pose_from_cache_entry(self.cache.latest(self.twin_uuid))


class SdkFlight:
    """:class:`FlightControls` over a Cyberwave drone twin (simulation only)."""

    def __init__(self, twin: Any) -> None:
        assert_client_simulation(twin.client)
        self.twin = twin

    def takeoff(self, altitude: float) -> None:
        """``twin.takeoff(altitude=)`` (MQTT ``command`` takeoff; verified live)."""
        assert_client_simulation(self.twin.client)
        self.twin.takeoff(altitude=altitude)

    def turn(self, rate: float, duration: float) -> None:
        """``locomotion.turn_left/turn_right(|rate|, duration=)`` burst (verified live)."""
        assert_client_simulation(self.twin.client)
        loco = self.twin.locomotion
        fn = loco.turn_left if rate > 0 else loco.turn_right
        fn(abs(rate), duration=duration)

    def forward(self, speed: float, duration: float) -> None:
        """``locomotion.move_forward(speed, duration=)`` burst (verified live)."""
        assert_client_simulation(self.twin.client)
        self.twin.locomotion.move_forward(speed, duration=duration)

    def stop(self) -> None:
        """``locomotion.stop()`` (the same ``stop`` every burst ends with)."""
        assert_client_simulation(self.twin.client)
        self.twin.locomotion.stop()

    def land(self) -> None:
        """``twin.land()`` (MQTT ``command`` land; not verified live before v1)."""
        assert_client_simulation(self.twin.client)
        self.twin.land()


class SdkCamera:
    """:class:`FrameGrabber` over ``twin.camera.get_frame`` for one sensor."""

    def __init__(self, twin: Any, sensor_id: str = BOTTOM_CAMERA) -> None:
        self.twin = twin
        self.sensor_id = sensor_id

    def grab(self) -> np.ndarray | None:
        """One usable frame or ``None`` (black / placeholder / not ready)."""
        frame = capture(self.twin, self.sensor_id)
        return frame if is_usable(frame) else None


__all__ = [
    "BOTTOM_CAMERA",
    "CachePoseSource",
    "FlightControls",
    "FrameGrabber",
    "Pose",
    "PoseSource",
    "SdkCamera",
    "SdkFlight",
    "camera_intrinsics",
    "pose_from_cache_entry",
    "wrap",
    "yaw_of",
]
