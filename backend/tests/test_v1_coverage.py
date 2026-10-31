"""Coverage: seen mask, pct_mapped and encodings consistent with stream E's OccupancyGrid."""

from __future__ import annotations

import base64

import numpy as np
import pytest

from skypaw.mission.coverage import BLOCKED, DIFFICULT, PASSABLE, Coverage, decode_cls, decode_seen
from skypaw.mission.grid import OccupancyGrid


def cls_at(cov: Coverage, cls: np.ndarray, x: float, y: float) -> int:
    i, j = cov.spec.cell_of(x, y)
    return int(cls[j, i])


def test_full_reveal_equals_grid_cls_bytes() -> None:
    cov = Coverage()
    cov.seen[:] = True
    assert base64.b64decode(cov.to_grid()["cls"]) == OccupancyGrid.from_site().to_cls_bytes()


@pytest.mark.parametrize(
    ("label", "x", "y", "expected"),
    [
        ("staging W", -12.5, -7.5, PASSABLE),
        ("grid corner (south-west)", -12.9, -8.9, PASSABLE),
        ("debris block (-7, 1), h 1.0", -7.0, 1.0, BLOCKED),
        ("debris block (6, -1.5), h 0.7", 6.0, -1.5, BLOCKED),
        ("debris block (2, 5), h 0.9", 2.0, 5.0, BLOCKED),
        ("slab (-5, 3), h 0.35", -4.8, 3.2, DIFFICULT),
        ("slab under V-03", 7.0, -4.8, DIFFICULT),
        ("buffer W of block (-7, 1)", -8.25, 0.75, DIFFICULT),
        ("open ground east of the site", 11.5, 0.0, PASSABLE),
        ("open ground north-east", 12.0, 8.5, PASSABLE),
    ],
)
def test_known_cells_once_seen(label: str, x: float, y: float, expected: int) -> None:
    cov = Coverage()
    cov.reveal(x, y, 1.0)
    cls = decode_cls(cov.to_grid()["cls"], cov.spec)
    assert cls_at(cov, cls, x, y) == expected, label


def test_unseen_cells_are_unmapped_and_row0_is_south() -> None:
    cov = Coverage()
    cls = decode_cls(cov.to_grid()["cls"], cov.spec)
    assert cls_at(cov, cls, -7.0, 1.0) == 0  # inside the footprint, not seen yet
    i, j = cov.spec.cell_of(0.0, -8.9)
    assert j == 0 and i == cov.spec.cell_of(0.0, 0.0)[0]


def test_reveal_and_pct() -> None:
    cov = Coverage()
    assert cov.pct_mapped() == 0
    assert cov.reveal(0.0, 0.0, 2.8) > 90  # ~ pi * 2.8^2 / 0.25 = 98 cells
    assert cov.reveal(0.0, 0.0, 2.8) == 0
    assert 0 < cov.pct_mapped() < 20
    for x in np.arange(-9, 9.5, 1.0):
        for y in np.arange(-6, 6.5, 1.0):
            cov.reveal(float(x), float(y), 1.0)
    assert cov.pct_mapped() == 100
    assert cov.reveal(100.0, 100.0, 2.8) == 0


def test_seen_is_msb_first_row_major() -> None:
    cov = Coverage()
    cov.reveal(-7.0, 1.0, 2.8)
    g = cov.to_grid()
    assert (g["x0"], g["y0"], g["cs"], g["w"], g["h"]) == (-13.0, -9.0, 0.5, 52, 36)
    assert np.array_equal(decode_seen(g["seen"], cov.spec), cov.seen)
    i, j = cov.spec.cell_of(-7.0, 1.0)
    idx = j * cov.spec.w + i
    raw = np.frombuffer(base64.b64decode(g["seen"]), dtype=np.uint8)
    assert (raw[idx // 8] >> (7 - idx % 8)) & 1 == 1
