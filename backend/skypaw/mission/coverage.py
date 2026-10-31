"""Occupancy classes from ``site.yaml`` and the drone's 0.5 m *seen* mask (PROTOCOL ``grid``).

Grid (world frame, PROTOCOL): ``x0=-13, y0=-9, cs=0.5, w=52, h=36``. Cell ``(i, j)`` covers
``x ∈ [x0 + i·cs, x0 + (i+1)·cs)`` and ``y ∈ [y0 + j·cs, y0 + (j+1)·cs)``; arrays are
``(h, w)`` and flattened **row-major with j (world +y) as the row**: ``index = j * w + i``.
``cls`` is one byte per cell (0 unmapped, 1 passable, 2 difficult, 3 blocked; unseen cells
are sent as 0). ``seen`` is a bitmask, bit ``index`` = byte ``index // 8``, bit
``index % 8`` (LSB first, ``numpy.packbits(bitorder="little")``). Both are base64.

Classification (:func:`classify_site`) is the approved mockup engine's rule
(``docs/mockups/ops-v2.html`` ``buildTruth``): a cell whose centre is inside a rubble box
(yaw-rotated footprint) gets *blocked* if the box height ≥ 0.6 m, else *difficult*; then
every passable interior cell with a blocked 8-neighbour becomes *difficult* (1-cell buffer).
Stream E (``grid.py`` / ``planner.py``) may import it.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from skypaw.scene.site import Site, load_site

UNMAPPED, PASSABLE, DIFFICULT, BLOCKED = 0, 1, 2, 3
BLOCKED_HEIGHT_M = 0.6


@dataclass(frozen=True)
class GridSpec:
    """World-frame grid bounds (PROTOCOL ``grid``)."""

    x0: float = -13.0
    y0: float = -9.0
    cs: float = 0.5
    w: int = 52
    h: int = 36

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        """``(i, j)`` of the cell containing world point ``(x, y)`` (may be out of range)."""
        return math.floor((x - self.x0) / self.cs), math.floor((y - self.y0) / self.cs)

    def centre(self, i: int, j: int) -> tuple[float, float]:
        """World ``(x, y)`` of the centre of cell ``(i, j)``."""
        return self.x0 + (i + 0.5) * self.cs, self.y0 + (j + 0.5) * self.cs

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        """``(X, Y)`` arrays of shape ``(h, w)`` with every cell centre."""
        xs = self.x0 + (np.arange(self.w) + 0.5) * self.cs
        ys = self.y0 + (np.arange(self.h) + 0.5) * self.cs
        return np.meshgrid(xs, ys)


GRID = GridSpec()


def classify_site(site: Site | None = None, spec: GridSpec = GRID) -> np.ndarray:
    """Ground-truth classes ``(h, w)`` uint8 (1/2/3) from the site's rubble boxes."""
    site = site or load_site()
    wx, wy = spec.centres()
    sx, sz = wx, -wy  # world (x, y) -> site (x, z)
    cls = np.full((spec.h, spec.w), PASSABLE, dtype=np.uint8)
    for r in site.rubble:
        dx, dz = sx - r.x, sz - r.z
        c, s = math.cos(-r.yaw), math.sin(-r.yaw)
        lx = dx * c - dz * s
        lz = dx * s + dz * c
        inside = (np.abs(lx) <= r.sx / 2) & (np.abs(lz) <= r.sz / 2)
        cls[inside] = np.maximum(cls[inside], BLOCKED if r.sy >= BLOCKED_HEIGHT_M else DIFFICULT)
    blocked = cls == BLOCKED
    near = np.zeros_like(blocked)
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            near[1:-1, 1:-1] |= blocked[1 + a : spec.h - 1 + a, 1 + b : spec.w - 1 + b]
    interior = np.zeros_like(blocked)
    interior[1:-1, 1:-1] = True
    cls[(cls == PASSABLE) & near & interior] = DIFFICULT
    return cls


def b64(data: bytes) -> str:
    """Standard base64 (with padding) as ASCII text."""
    return base64.b64encode(data).decode("ascii")


class Coverage:
    """Seen mask over :data:`GRID` plus the ground-truth classes it reveals."""

    #: Footprint used for ``pct_mapped`` (world |x| ≤ 9, |y| ≤ 6; mockup ``mappedPct``).
    FOOTPRINT = (9.0, 6.0)
    #: Cells outside this box are pre-seen at reset (mockup ``resetSeen``: |x|>9.8, |z|>6.8).
    PRESEEN_OUTSIDE = (9.8, 6.8)

    def __init__(self, truth: np.ndarray | None = None, spec: GridSpec = GRID) -> None:
        self.spec = spec
        self.truth = classify_site(spec=spec) if truth is None else truth.astype(np.uint8)
        if self.truth.shape != (spec.h, spec.w):
            raise ValueError(f"truth shape {self.truth.shape} != {(spec.h, spec.w)}")
        x, y = spec.centres()
        fx, fy = self.FOOTPRINT
        self._in_footprint = (np.abs(x) <= fx) & (np.abs(y) <= fy)
        self.seen = np.zeros((spec.h, spec.w), dtype=bool)
        self.reset()

    def reset(self) -> None:
        """Clear the seen mask (cells well outside the footprint start seen, as in the mockup)."""
        x, y = self.spec.centres()
        px, py = self.PRESEEN_OUTSIDE
        self.seen[:] = (np.abs(x) > px) | (np.abs(y) > py)

    def reveal(self, x: float, y: float, r: float) -> int:
        """Mark every cell whose centre is within ``r`` m of world ``(x, y)``; returns new cells."""
        s = self.spec
        ci, cj = s.cell_of(x, y)
        n = math.ceil(r / s.cs)
        i0, i1 = max(0, ci - n), min(s.w - 1, ci + n)
        j0, j1 = max(0, cj - n), min(s.h - 1, cj + n)
        if i0 > i1 or j0 > j1:
            return 0
        xs = s.x0 + (np.arange(i0, i1 + 1) + 0.5) * s.cs
        ys = s.y0 + (np.arange(j0, j1 + 1) + 0.5) * s.cs
        gx, gy = np.meshgrid(xs, ys)
        hit = np.hypot(gx - x, gy - y) <= r
        window = self.seen[j0 : j1 + 1, i0 : i1 + 1]
        new = int((hit & ~window).sum())
        window |= hit
        return new

    def pct_mapped(self) -> int:
        """Rounded percentage of footprint cells seen (0..100)."""
        total = int(self._in_footprint.sum())
        return round(100 * int((self.seen & self._in_footprint).sum()) / total) if total else 0

    def cls_visible(self) -> np.ndarray:
        """Classes as the console may show them: truth where seen, 0 elsewhere."""
        return np.where(self.seen, self.truth, UNMAPPED).astype(np.uint8)

    def cls_b64(self) -> str:
        """``cls`` field: base64 of ``w*h`` bytes, row-major (j rows)."""
        return b64(self.cls_visible().tobytes(order="C"))

    def seen_b64(self) -> str:
        """``seen`` field: base64 of the LSB-first packed bitmask, row-major."""
        return b64(np.packbits(self.seen.ravel(order="C"), bitorder="little").tobytes())

    def to_grid(self) -> dict[str, Any]:
        """The PROTOCOL ``grid`` object."""
        s = self.spec
        return {
            "x0": s.x0,
            "y0": s.y0,
            "cs": s.cs,
            "w": s.w,
            "h": s.h,
            "cls": self.cls_b64(),
            "seen": self.seen_b64(),
        }


def decode_cls(data: str, spec: GridSpec = GRID) -> np.ndarray:
    """Inverse of :meth:`Coverage.cls_b64` (tests and tools)."""
    return np.frombuffer(base64.b64decode(data), dtype=np.uint8).reshape(spec.h, spec.w)


def decode_seen(data: str, spec: GridSpec = GRID) -> np.ndarray:
    """Inverse of :meth:`Coverage.seen_b64` (tests and tools)."""
    bits = np.unpackbits(np.frombuffer(base64.b64decode(data), dtype=np.uint8), bitorder="little")
    return bits[: spec.w * spec.h].astype(bool).reshape(spec.h, spec.w)


__all__ = [
    "BLOCKED",
    "DIFFICULT",
    "GRID",
    "PASSABLE",
    "UNMAPPED",
    "Coverage",
    "GridSpec",
    "b64",
    "classify_site",
    "decode_cls",
    "decode_seen",
]
