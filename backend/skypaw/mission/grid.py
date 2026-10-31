"""Occupancy grid over the site in the Cyberwave world frame (PROTOCOL "grid", SPEC §4, §6.4).

Geometry: world x in [x0, x0 + w*cs), y in [y0, y0 + h*cs); defaults x0 = -13, y0 = -9,
cs = 0.5, w = 52, h = 36. Cell ``(i, j)`` covers ``[x0 + i*cs, x0 + (i+1)*cs)`` by
``[y0 + j*cs, y0 + (j+1)*cs)``. Byte order on the wire (``to_cls_bytes``) is row-major
with rows by **increasing world y**: ``index = j * w + i`` (row 0 is the south edge, y = y0).

Classes (PROTOCOL): 0 unmapped, 1 passable, 2 difficult, 3 blocked.

Built from ``site.yaml`` rubble boxes converted to world (``site_to_world``: X = x, Y = -z,
yaw about +Z unchanged). A cell whose square overlaps a box footprint by a positive area is
"under" it: boxes with height >= 0.6 m make it blocked, lower boxes (slabs) difficult.
Every non-blocked cell 8-adjacent to a blocked cell is difficult (a one-cell buffer).

This module is self-contained on purpose: the drone's seen-mask lives in
``skypaw.mission.coverage`` (stream A) and is passed in here as a plain boolean array.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from skypaw.scene.site import RubbleBox, Site, load_site

UNMAPPED = 0
PASSABLE = 1
DIFFICULT = 2
BLOCKED = 3

#: Box height (m) from which a rubble box blocks the cells under it (SPEC §4).
BLOCK_HEIGHT_M = 0.6
_EPS = 1e-9

Point = tuple[float, float]


@dataclass(frozen=True)
class GridSpec:
    """Grid placement in the world frame (PROTOCOL ``grid`` fields)."""

    x0: float = -13.0
    y0: float = -9.0
    cs: float = 0.5
    w: int = 52
    h: int = 36

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        """``(i, j)`` of the cell containing world ``(x, y)``; may be out of bounds."""
        return (math.floor((x - self.x0) / self.cs), math.floor((y - self.y0) / self.cs))

    def centre(self, i: int, j: int) -> Point:
        """World ``(x, y)`` of the centre of cell ``(i, j)``."""
        return (self.x0 + (i + 0.5) * self.cs, self.y0 + (j + 0.5) * self.cs)

    def in_bounds(self, i: int, j: int) -> bool:
        """True if ``(i, j)`` is inside the grid."""
        return 0 <= i < self.w and 0 <= j < self.h

    @classmethod
    def from_site(cls, site: Site) -> GridSpec:
        """Read ``site.yaml``'s ``grid`` block (site z maps to world -y)."""
        g = site.grid
        cs = float(g.get("cell", 0.5))
        w, h = int(g.get("w", 52)), int(g.get("h", 36))
        # site z in [z0, z0 + h*cs) maps to world y in (-(z0 + h*cs), -z0].
        y0 = -(float(g.get("z0", -9.0)) + h * cs) + 0.0
        return cls(x0=float(g.get("x0", -13.0)), y0=y0, cs=cs, w=w, h=h)


def box_footprint_world(box: RubbleBox) -> list[Point]:
    """The four ground corners (world x, y) of a rubble box, counter-clockwise."""
    cx, cy = box.x, -box.z + 0.0
    hx, hy = box.sx / 2.0, box.sz / 2.0
    c, s = math.cos(box.yaw), math.sin(box.yaw)
    return [
        (cx + c * dx - s * dy, cy + s * dx + c * dy)
        for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
    ]


def _axes(poly: Sequence[Point]) -> list[Point]:
    out = []
    for k in range(len(poly)):
        (x1, y1), (x2, y2) = poly[k], poly[(k + 1) % len(poly)]
        out.append((-(y2 - y1), x2 - x1))
    return out


def polygons_overlap(a: Sequence[Point], b: Sequence[Point]) -> bool:
    """Separating-axis test for two convex polygons; touching edges do not count."""
    for ax, ay in _axes(a) + _axes(b):
        norm = math.hypot(ax, ay) or 1.0
        pa = [(x * ax + y * ay) / norm for x, y in a]
        pb = [(x * ax + y * ay) / norm for x, y in b]
        if min(max(pa), max(pb)) - max(min(pa), min(pb)) <= _EPS:
            return False
    return True


class OccupancyGrid:
    """Classified grid (``cls[j, i]``, uint8) with an optional seen mask and hazards."""

    def __init__(
        self,
        spec: GridSpec,
        truth: np.ndarray,
        *,
        seen: np.ndarray | None = None,
        respect_seen: bool = False,
        hazards: Iterable[Point] = (),
    ) -> None:
        if truth.shape != (spec.h, spec.w):
            raise ValueError(f"truth must have shape {(spec.h, spec.w)}, got {truth.shape}")
        self.spec = spec
        self.truth = truth.astype(np.uint8, copy=True)
        self.respect_seen = respect_seen
        self.seen: np.ndarray | None = None
        self.hazards: tuple[Point, ...] = tuple((float(x), float(y)) for x, y in hazards)
        self.set_seen(seen)

    @classmethod
    def from_site(
        cls,
        site: Site | None = None,
        *,
        spec: GridSpec | None = None,
        seen: np.ndarray | None = None,
        respect_seen: bool = False,
        hazards: Iterable[Point] = (),
    ) -> OccupancyGrid:
        """Classify every cell from the site's rubble boxes (see the module docstring)."""
        site = site or load_site()
        spec = spec or GridSpec.from_site(site)
        return cls(
            spec,
            classify_boxes(spec, site.rubble),
            seen=seen,
            respect_seen=respect_seen,
            hazards=hazards,
        )

    def set_seen(self, seen: np.ndarray | None) -> None:
        """Replace the seen mask (bool array ``(h, w)``, True = seen), or clear it."""
        if seen is None:
            self.seen = None
            return
        arr = np.asarray(seen, dtype=bool)
        if arr.shape != (self.spec.h, self.spec.w):
            raise ValueError(f"seen must have shape {(self.spec.h, self.spec.w)}, got {arr.shape}")
        self.seen = arr.copy()

    @property
    def cls(self) -> np.ndarray:
        """Effective classes: truth, with unseen cells UNMAPPED when ``respect_seen``."""
        if self.respect_seen and self.seen is not None:
            out = self.truth.copy()
            out[~self.seen] = UNMAPPED
            return out
        return self.truth

    def class_at(self, x: float, y: float) -> int:
        """Effective class of the cell containing world ``(x, y)``; BLOCKED outside the grid."""
        i, j = self.spec.cell_of(x, y)
        if not self.spec.in_bounds(i, j):
            return BLOCKED
        return int(self.cls[j, i])

    def traversable_mask(self) -> np.ndarray:
        """True where a robot may route (passable or difficult); unmapped/blocked are not."""
        c = self.cls
        return (c == PASSABLE) | (c == DIFFICULT)

    def to_cls_bytes(self) -> bytes:
        """``w*h`` bytes, row-major, rows by increasing world y (PROTOCOL ``grid.cls``)."""
        return np.ascontiguousarray(self.cls, dtype=np.uint8).tobytes()

    def to_cls_b64(self) -> str:
        """Base64 of :meth:`to_cls_bytes`, as it goes into the snapshot."""
        return base64.b64encode(self.to_cls_bytes()).decode("ascii")

    def to_snapshot(self) -> dict[str, object]:
        """The PROTOCOL ``grid`` object without ``seen`` (stream A adds the seen bitmask)."""
        s = self.spec
        return {"x0": s.x0, "y0": s.y0, "cs": s.cs, "w": s.w, "h": s.h, "cls": self.to_cls_b64()}

    def render(self, marks: dict[tuple[int, int], str] | None = None) -> str:
        """ASCII picture, north (high y) at the top: ``? . ~ #`` for classes 0..3."""
        glyph = "?.~#"
        marks = marks or {}
        c = self.cls
        rows = []
        for j in range(self.spec.h - 1, -1, -1):
            rows.append("".join(marks.get((i, j), glyph[int(c[j, i])]) for i in range(self.spec.w)))
        return "\n".join(rows)


def _dilate8(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    out = mask.copy()
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            if dj == 0 and di == 0:
                continue
            shifted = np.zeros_like(mask)
            shifted[max(dj, 0) : h + min(dj, 0), max(di, 0) : w + min(di, 0)] = mask[
                max(-dj, 0) : h + min(-dj, 0), max(-di, 0) : w + min(-di, 0)
            ]
            out |= shifted
    return out


def classify_boxes(spec: GridSpec, boxes: Iterable[RubbleBox]) -> np.ndarray:
    """Truth classes ``(h, w)`` from rubble boxes: blocked, difficult (slab or buffer), passable."""
    blocked = np.zeros((spec.h, spec.w), dtype=bool)
    slab = np.zeros((spec.h, spec.w), dtype=bool)
    for box in boxes:
        poly = box_footprint_world(box)
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        i0, j0 = spec.cell_of(min(xs), min(ys))
        i1, j1 = spec.cell_of(max(xs), max(ys))
        target = blocked if box.sy >= BLOCK_HEIGHT_M else slab
        for j in range(max(j0, 0), min(j1, spec.h - 1) + 1):
            for i in range(max(i0, 0), min(i1, spec.w - 1) + 1):
                x, y = spec.x0 + i * spec.cs, spec.y0 + j * spec.cs
                square = [(x, y), (x + spec.cs, y), (x + spec.cs, y + spec.cs), (x, y + spec.cs)]
                if polygons_overlap(poly, square):
                    target[j, i] = True
    out = np.full((spec.h, spec.w), PASSABLE, dtype=np.uint8)
    out[slab | _dilate8(blocked)] = DIFFICULT
    out[blocked] = BLOCKED
    return out


__all__ = [
    "BLOCKED",
    "BLOCK_HEIGHT_M",
    "DIFFICULT",
    "PASSABLE",
    "UNMAPPED",
    "GridSpec",
    "OccupancyGrid",
    "box_footprint_world",
    "classify_boxes",
    "polygons_overlap",
]
