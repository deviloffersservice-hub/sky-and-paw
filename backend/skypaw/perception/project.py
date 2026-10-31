"""Pixel -> world ground point for a camera with a known pose (no depth needed).

Frames and conventions (also see docs/PROTOCOL.md "Coordinates and units"):

* **World** = Cyberwave / MuJoCo world: X east, Y north, **Z up**, metres. Frame poses arrive
  as MQTT ``position`` ``[x, y, z]`` and ``rotation`` quaternion, which ``ports.Frame``
  stores as ``[w, x, y, z]`` (scalar first). The quaternion rotates body -> world.
* **Body** = the drone body: +x forward, +y left, +z up (level flight: body z = world Z).
* **Camera (OpenCV)** = +x image right, +y image down, +z along the optical axis.
* **Mount** = the camera axes expressed in the body frame (3x3, columns = camera x, y, z).
  The Skydio X2 ``camera_chasis_bottom`` looks straight down (optical axis = body -z when
  level). Its asset offset rotation is 90 deg about z (``w = z = -0.7071``) on a MuJoCo camera
  (looks along its -z, image up = its +y), so **image right = body +y and image down = body
  +x** (:data:`MOUNT_DOWN`, ``"up_back"``). Verified on the real M2 overhead frame
  (tests/evidence/M2/dev/overhead_bottom.jpg): the site.yaml rubble tops projected with this
  mount, fovy 58.72 deg and the MQTT yaw overlap the bright slabs with IoU 0.88; the other
  three in-plane orientations score lower (``backend/tests/test_projection.py``). That frame
  was ~2.7 m stale along the flight path, so live capture is done with the drone stationary.
* **Intrinsics**: pinhole, square pixels. ``fov`` is MuJoCo's ``fovy`` (vertical field of
  view, 58.72 deg for the X2 cameras), so for 640x360 ``f = 180 / tan(29.36 deg) = 320 px``
  (horizontal fov 90 deg). Principal point at the image centre.

The X2 has no depth camera in MuJoCo, so a pixel ray is intersected with a horizontal plane
``Z = plane_z`` (the flat footprint, or the expected target height). :func:`world_to_pixel` is
the exact inverse and is used by the tests for the round trip.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: MuJoCo fovy of the Skydio X2 cameras (Cyberwave asset), degrees.
DEFAULT_FOVY_DEG = 58.72
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360


def _mount(right: Sequence[float], down: Sequence[float]) -> np.ndarray:
    """Mount matrix for a downward camera from the body directions of image right/down."""
    r, d = np.asarray(right, dtype=float), np.asarray(down, dtype=float)
    return np.column_stack([r, d, np.cross(r, d)])


#: Candidate in-plane orientations of a straight-down camera (optical axis = body -z).
MOUNTS: dict[str, np.ndarray] = {
    # image right = body +x, image down = body -y (MuJoCo camera frame = body frame)
    "right_fwd": _mount([1, 0, 0], [0, -1, 0]),
    # image up = body +x (forward), image right = body -y
    "up_fwd": _mount([0, -1, 0], [-1, 0, 0]),
    "right_back": _mount([-1, 0, 0], [0, 1, 0]),
    "up_back": _mount([0, 1, 0], [1, 0, 0]),
}
MOUNT_DOWN_NAME = "up_back"
MOUNT_DOWN = MOUNTS[MOUNT_DOWN_NAME]


@dataclass(frozen=True)
class Intrinsics:
    """Pinhole intrinsics; ``fx = fy`` from the vertical field of view."""

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fovy_deg: float = DEFAULT_FOVY_DEG

    @property
    def f(self) -> float:
        """Focal length in pixels."""
        return (self.height / 2.0) / math.tan(math.radians(self.fovy_deg) / 2.0)

    @property
    def cx(self) -> float:
        return self.width / 2.0

    @property
    def cy(self) -> float:
        return self.height / 2.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Intrinsics:
        """From a frame's ``intrinsics`` dict (keys ``width``, ``height``, ``fovy``/``fov``)."""
        raw = raw or {}
        fov = raw.get("fovy", raw.get("fov", DEFAULT_FOVY_DEG))
        return cls(
            width=int(raw.get("width", DEFAULT_WIDTH)),
            height=int(raw.get("height", DEFAULT_HEIGHT)),
            fovy_deg=float(fov),
        )


def quat_wxyz_to_matrix(q: Sequence[float]) -> np.ndarray:
    """Rotation matrix (body -> world) of a quaternion ``[w, x, y, z]`` (normalised here)."""
    w, x, y, z = (float(v) for v in q)
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n == 0.0:
        raise ValueError("zero quaternion")
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def yaw_to_quat_wxyz(yaw: float) -> list[float]:
    """Quaternion ``[w, x, y, z]`` for a rotation of ``yaw`` rad about world +Z."""
    return [math.cos(yaw / 2.0), 0.0, 0.0, math.sin(yaw / 2.0)]


def rotation_wxyz(rotation: Any) -> list[float]:
    """Accept ``[w, x, y, z]`` or an MQTT dict ``{w, x, y, z}``; return ``[w, x, y, z]``."""
    if isinstance(rotation, dict):
        return [float(rotation.get(k, 0.0)) for k in "wxyz"]
    vals = [float(v) for v in rotation]
    if len(vals) != 4:
        raise ValueError(f"rotation must have 4 components [w, x, y, z], got {rotation!r}")
    return vals


def position_xyz(position: Any) -> list[float]:
    """Accept ``[x, y, z]`` or an MQTT dict ``{x, y, z}``; return ``[x, y, z]``."""
    if isinstance(position, dict):
        return [float(position[k]) for k in "xyz"]
    vals = [float(v) for v in position]
    if len(vals) != 3:
        raise ValueError(f"position must be [x, y, z], got {position!r}")
    return vals


@dataclass(frozen=True)
class CameraPose:
    """Camera centre and orientation in the world frame."""

    position: np.ndarray  # (3,)
    r_world_cam: np.ndarray  # (3, 3): columns = camera axes in world
    intr: Intrinsics = field(default_factory=Intrinsics)

    @classmethod
    def from_frame(
        cls,
        position: Any,
        rotation: Any,
        intrinsics: dict[str, Any] | Intrinsics | None = None,
        mount: np.ndarray = MOUNT_DOWN,
    ) -> CameraPose:
        """From a frame's ``position``/``rotation`` (body pose) and a body->camera mount."""
        r_wb = quat_wxyz_to_matrix(rotation_wxyz(rotation))
        intr = (
            intrinsics if isinstance(intrinsics, Intrinsics) else Intrinsics.from_dict(intrinsics)
        )
        return cls(np.asarray(position_xyz(position), dtype=float), r_wb @ mount, intr)


def pixel_to_world(
    pose: CameraPose, u: float, v: float, plane_z: float = 0.0
) -> tuple[float, float, float] | None:
    """World point where the ray through pixel ``(u, v)`` meets the plane ``Z = plane_z``.

    Returns ``None`` if the ray is parallel to the plane or points away from it.
    """
    i = pose.intr
    d_cam = np.array([(u - i.cx) / i.f, (v - i.cy) / i.f, 1.0])
    d = pose.r_world_cam @ d_cam
    if abs(d[2]) < 1e-9:
        return None
    s = (plane_z - pose.position[2]) / d[2]
    if s <= 0:
        return None
    p = pose.position + s * d
    return (float(p[0]), float(p[1]), float(plane_z))


def world_to_pixel(pose: CameraPose, point: Sequence[float]) -> tuple[float, float] | None:
    """Pixel ``(u, v)`` of a world point, or ``None`` if it is behind the camera."""
    rel = np.asarray(point, dtype=float) - pose.position
    c = pose.r_world_cam.T @ rel
    if c[2] <= 1e-9:
        return None
    i = pose.intr
    return (float(i.cx + i.f * c[0] / c[2]), float(i.cy + i.f * c[1] / c[2]))


def ground_footprint(pose: CameraPose, plane_z: float = 0.0) -> list[tuple[float, float]]:
    """World (X, Y) of the four image corners on the plane (for coverage and debugging)."""
    i = pose.intr
    out = []
    for u, v in ((0, 0), (i.width, 0), (i.width, i.height), (0, i.height)):
        p = pixel_to_world(pose, u, v, plane_z)
        if p is not None:
            out.append((p[0], p[1]))
    return out


__all__ = [
    "DEFAULT_FOVY_DEG",
    "MOUNTS",
    "MOUNT_DOWN",
    "MOUNT_DOWN_NAME",
    "CameraPose",
    "Intrinsics",
    "ground_footprint",
    "pixel_to_world",
    "position_xyz",
    "quat_wxyz_to_matrix",
    "rotation_wxyz",
    "world_to_pixel",
    "yaw_to_quat_wxyz",
]
