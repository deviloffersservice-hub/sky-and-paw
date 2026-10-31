"""Coverage grid: classification (mockup rule), reveal, pct_mapped, base64 encodings."""

from __future__ import annotations

import math

import numpy as np
import pytest

from skypaw.mission.coverage import (
    BLOCKED,
    DIFFICULT,
    GRID,
    PASSABLE,
    Coverage,
    classify_site,
    decode_cls,
    decode_seen,
)
from skypaw.scene.site import load_site


def mockup_truth() -> np.ndarray:
    """Literal port of ops-v2.html buildTruth (site frame, rows = site z), then flipped to world."""
    site = load_site()
    w, h, cs, x0, z0 = 52, 36, 0.5, -13.0, -9.0
    t = np.zeros((h, w), dtype=np.uint8)
    for cz in range(h):
        for cx in range(w):
            x, z = x0 + (cx + 0.5) * cs, z0 + (cz + 0.5) * cs
            cls = 1
            for r in site.rubble:
                dx, dz = x - r.x, z - r.z
                c, s = math.cos(-r.yaw), math.sin(-r.yaw)
                lx, lz = dx * c - dz * s, dx * s + dz * c
                if abs(lx) <= r.sx / 2 and abs(lz) <= r.sz / 2:
                    cls = max(cls, 3 if r.sy >= 0.6 else 2)
            t[cz, cx] = cls
    copy = t.copy()
    for cz in range(1, h - 1):
        for cx in range(1, w - 1):
            if copy[cz, cx] == 1 and (copy[cz - 1 : cz + 2, cx - 1 : cx + 2] == 3).any():
                t[cz, cx] = 2
    return t[::-1, :]  # site z rows -> world y rows (y = -z)


def cls_at(cls: np.ndarray, x: float, y: float) -> int:
    i, j = GRID.cell_of(x, y)
    return int(cls[j, i])


def test_classification_matches_mockup_everywhere() -> None:
    assert np.array_equal(classify_site(), mockup_truth())


@pytest.mark.parametrize(
    ("label", "x", "y", "expected"),
    [
        ("staging W", -12.5, -7.5, PASSABLE),
        ("grid corner", -12.9, -8.9, PASSABLE),
        ("debris block (-7, 1), h 1.0", -7.2, 0.8, BLOCKED),
        ("debris block (6, -1.5), h 0.7", 6.1, -1.4, BLOCKED),
        ("debris block (2, 5), h 0.9", 2.0, 5.0, BLOCKED),
        ("slab (-5, 3), h 0.35", -4.8, 3.2, DIFFICULT),
        ("slab under V-03", 7.0, -4.8, DIFFICULT),
        ("buffer W of block (-7, 1)", -8.25, 0.75, DIFFICULT),
        ("buffer E of block (-7, 1)", -5.75, 1.25, DIFFICULT),
        ("open ground E of the x=9 wall line", 9.0, 2.0, PASSABLE),
    ],
)
def test_known_cells(label: str, x: float, y: float, expected: int) -> None:
    assert cls_at(classify_site(), x, y) == expected, label


def test_counts_and_shape() -> None:
    cls = classify_site()
    assert cls.shape == (GRID.h, GRID.w)
    assert set(np.unique(cls)) == {PASSABLE, DIFFICULT, BLOCKED}


def test_reveal_and_pct() -> None:
    cov = Coverage()
    assert cov.pct_mapped() == 0
    new = cov.reveal(0.0, 0.0, 2.8)
    assert new > 90  # ~ pi * 2.8^2 / 0.25 = 98 cells
    assert cov.reveal(0.0, 0.0, 2.8) == 0
    assert 0 < cov.pct_mapped() < 20
    for x in np.arange(-9, 9.5, 1.0):
        for y in np.arange(-6, 6.5, 1.0):
            cov.reveal(float(x), float(y), 1.0)
    assert cov.pct_mapped() == 100


def test_reveal_out_of_grid_is_safe() -> None:
    cov = Coverage()
    assert cov.reveal(100.0, 100.0, 2.8) == 0


def test_encodings_roundtrip() -> None:
    cov = Coverage()
    cov.reveal(-7.0, 1.0, 2.8)
    g = cov.to_grid()
    assert (g["x0"], g["y0"], g["cs"], g["w"], g["h"]) == (-13.0, -9.0, 0.5, 52, 36)
    cls = decode_cls(g["cls"])
    seen = decode_seen(g["seen"])
    assert np.array_equal(seen, cov.seen)
    assert np.array_equal(cls[seen], cov.truth[seen])
    assert (cls[~seen] == 0).all()
    assert cls_at(cls, -7.2, 0.8) == BLOCKED
    # bit order: LSB first, row-major index j*w + i
    i, j = GRID.cell_of(-7.2, 0.8)
    idx = j * GRID.w + i
    raw = np.frombuffer(__import__("base64").b64decode(g["seen"]), dtype=np.uint8)
    assert (raw[idx // 8] >> (idx % 8)) & 1 == 1
