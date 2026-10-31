"""The drone's 0.5 m *seen* mask over the PROTOCOL grid, with classes from stream E's grid.

Classes come from :class:`skypaw.mission.grid.OccupancyGrid` (stream E, ``from_site``), so
``grid.cls`` here is byte-for-byte ``OccupancyGrid.to_cls_bytes`` with unseen cells sent as
0 (unmapped). Encoding (PROTOCOL "Clarifications"): row-major ``index = j*w + i``, row 0 =
south edge (``y = y0 + (j+0.5)*cs``); ``seen`` is a bitmask packed MSB-first
(``numpy.packbits`` default order) over the same indexing; both base64.
"""

from __future__ import annotations

import base64
import math
from typing import Any

import numpy as np

from skypaw.mission.grid import BLOCKED, DIFFICULT, PASSABLE, UNMAPPED, GridSpec, OccupancyGrid


def b64(data: bytes) -> str:
    """Standard base64 (with padding) as ASCII text."""
    return base64.b64encode(data).decode("ascii")


class Coverage:
    """Seen mask plus the ground-truth classes it reveals."""

    #: Footprint used for ``pct_mapped`` (world |x| ≤ 9, |y| ≤ 6; mockup ``mappedPct``).
    FOOTPRINT = (9.0, 6.0)
    #: Cells outside this box are pre-seen at reset (mockup ``resetSeen``: |x|>9.8, |y|>6.8).
    PRESEEN_OUTSIDE = (9.8, 6.8)

    def __init__(self, grid: OccupancyGrid | None = None) -> None:
        self.grid = grid or OccupancyGrid.from_site()
        self.spec: GridSpec = self.grid.spec
        self.truth = self.grid.truth
        x, y = self._centres()
        fx, fy = self.FOOTPRINT
        self._in_footprint = (np.abs(x) <= fx) & (np.abs(y) <= fy)
        self.seen = np.zeros((self.spec.h, self.spec.w), dtype=bool)
        self.reset()

    def _centres(self) -> tuple[np.ndarray, np.ndarray]:
        s = self.spec
        xs = s.x0 + (np.arange(s.w) + 0.5) * s.cs
        ys = s.y0 + (np.arange(s.h) + 0.5) * s.cs
        return np.meshgrid(xs, ys)

    def reset(self) -> None:
        """Clear the seen mask (cells well outside the footprint start seen, as in the mockup)."""
        x, y = self._centres()
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
        """``cls`` field: base64 of ``w*h`` bytes, row-major, row 0 = south."""
        return b64(np.ascontiguousarray(self.cls_visible()).tobytes())

    def seen_b64(self) -> str:
        """``seen`` field: base64 of the MSB-first packed bitmask, row-major."""
        return b64(np.packbits(self.seen.ravel(order="C")).tobytes())

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


def decode_cls(data: str, spec: GridSpec) -> np.ndarray:
    """Inverse of :meth:`Coverage.cls_b64` (tests and tools)."""
    return np.frombuffer(base64.b64decode(data), dtype=np.uint8).reshape(spec.h, spec.w)


def decode_seen(data: str, spec: GridSpec) -> np.ndarray:
    """Inverse of :meth:`Coverage.seen_b64` (tests and tools)."""
    bits = np.unpackbits(np.frombuffer(base64.b64decode(data), dtype=np.uint8))
    return bits[: spec.w * spec.h].astype(bool).reshape(spec.h, spec.w)


__all__ = [
    "BLOCKED",
    "DIFFICULT",
    "PASSABLE",
    "UNMAPPED",
    "Coverage",
    "b64",
    "decode_cls",
    "decode_seen",
]
