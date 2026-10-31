"""Site geometry loaded from ``site.yaml``, plus the site <-> Cyberwave world frame mapping.

Frames (also documented at the top of ``site.yaml``):

* **site frame**: the mockup / console frame (three.js): x east, **y up**, z south.
* **world frame**: Cyberwave / MuJoCo: X east, Y north, **Z up** (right-handed).

``site_to_world(x, y, z) = (x, -z, y)``; a yaw about site +y equals the same yaw about
world +Z. Waypoints, twin editor poses and the GLB vertices are written in the world frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

SCENE_DIR = Path(__file__).resolve().parent
DEFAULT_SITE_PATH = SCENE_DIR / "site.yaml"
DEFAULT_GLB_PATH = SCENE_DIR / "rubble.glb"

VertexFrame = Literal["world_z_up", "gltf_y_up"]
Vec3 = tuple[float, float, float]


def site_to_world(x: float, y: float, z: float) -> Vec3:
    """Map a site-frame point (y up) to the Cyberwave world frame (Z up)."""
    return (float(x), -float(z) + 0.0, float(y))


def world_to_site(x: float, y: float, z: float) -> Vec3:
    """Inverse of :func:`site_to_world`."""
    return (float(x), float(z), -float(y) + 0.0)


def yaw_quaternion(yaw: float) -> dict[str, float]:
    """Quaternion ``{x, y, z, w}`` for a rotation of ``yaw`` radians about world +Z."""
    half = float(yaw) / 2.0
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


@dataclass(frozen=True)
class RubbleBox:
    """One rubble box: centre on the ground at site (x, z), size sx * sy (height) * sz."""

    x: float
    z: float
    sx: float
    sy: float
    sz: float
    yaw: float


@dataclass(frozen=True)
class SitePose:
    """A spawn pose: position in the site frame and yaw (radians)."""

    pos: Vec3
    yaw: float = 0.0

    @property
    def world(self) -> Vec3:
        """The position in the Cyberwave world frame."""
        return site_to_world(*self.pos)


@dataclass(frozen=True)
class Survey:
    """Drone lawnmower survey parameters (SPEC §6.3)."""

    x0: float
    x1: float
    rows: tuple[float, ...]
    alt: float
    speed: float

    def corners(self) -> list[Vec3]:
        """Lawnmower turn points in flight order, site frame: 2 per row, alternating."""
        pts: list[Vec3] = []
        for i, z in enumerate(self.rows):
            a, b = (self.x1, self.x0) if i % 2 else (self.x0, self.x1)
            pts.append((a, self.alt, z))
            pts.append((b, self.alt, z))
        return pts


@dataclass(frozen=True)
class Site:
    """Everything in ``site.yaml``, typed."""

    rubble: tuple[RubbleBox, ...]
    staging: tuple[float, float]
    survey: Survey
    spawn: dict[str, SitePose]
    push_test_go2: SitePose
    victims: tuple[dict[str, Any], ...]
    footprint: dict[str, float]
    grid: dict[str, float]
    glb_vertex_frame: VertexFrame = "world_z_up"
    source: Path = field(default=DEFAULT_SITE_PATH, compare=False)

    @property
    def staging_world(self) -> Vec3:
        """Staging point on the ground, world frame."""
        return site_to_world(self.staging[0], 0.0, self.staging[1])


def _pose(raw: dict[str, Any]) -> SitePose:
    pos = raw["pos"]
    if len(pos) != 3:
        raise ValueError(f"pose position must be [x, y, z], got {pos!r}")
    return SitePose(pos=(float(pos[0]), float(pos[1]), float(pos[2])), yaw=float(raw.get("yaw", 0)))


def load_site(path: Path | str = DEFAULT_SITE_PATH) -> Site:
    """Parse ``site.yaml`` into a :class:`Site`; raises ``ValueError`` on malformed data."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rubble = []
    for row in data["rubble"]:
        if len(row) != 6:
            raise ValueError(f"rubble rows are (x, z, sx, sy, sz, yaw), got {row!r}")
        box = RubbleBox(*(float(v) for v in row))
        if min(box.sx, box.sy, box.sz) <= 0:
            raise ValueError(f"rubble box sizes must be positive: {row!r}")
        rubble.append(box)
    sv = data["survey"]
    survey = Survey(
        x0=float(sv["x0"]),
        x1=float(sv["x1"]),
        rows=tuple(float(r) for r in sv["rows"]),
        alt=float(sv["alt"]),
        speed=float(sv["speed"]),
    )
    frame = data.get("glb", {}).get("vertex_frame", "world_z_up")
    if frame not in ("world_z_up", "gltf_y_up"):
        raise ValueError(f"glb.vertex_frame must be world_z_up or gltf_y_up, got {frame!r}")
    st = data["staging"]
    return Site(
        rubble=tuple(rubble),
        staging=(float(st["x"]), float(st["z"])),
        survey=survey,
        spawn={name: _pose(p) for name, p in data["spawn"].items()},
        push_test_go2=_pose(data["push_test"]["go2"]),
        victims=tuple(data["victims"]),
        footprint={k: float(v) for k, v in data["footprint"].items()},
        grid={k: float(v) for k, v in data["grid"].items()},
        glb_vertex_frame=frame,
        source=path,
    )


__all__ = [
    "DEFAULT_GLB_PATH",
    "DEFAULT_SITE_PATH",
    "RubbleBox",
    "Site",
    "SitePose",
    "Survey",
    "load_site",
    "site_to_world",
    "world_to_site",
    "yaw_quaternion",
]
