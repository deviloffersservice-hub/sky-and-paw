"""Stream E: occupancy grid classification and the Dijkstra planner on the real site."""

from __future__ import annotations

import base64
import math

import numpy as np
import pytest

from skypaw.mission.grid import (
    BLOCKED,
    DIFFICULT,
    PASSABLE,
    UNMAPPED,
    GridSpec,
    OccupancyGrid,
    polygons_overlap,
)
from skypaw.mission.planner import GO2, PERSON, NoRoute, plan_route, simplify
from skypaw.scene.site import load_site, site_to_world

SITE = load_site()
VICTIMS = {v["id"]: site_to_world(*v["pos"]) for v in SITE.victims}
STAGING = (-12.5, -7.5)


@pytest.fixture(scope="module")
def grid() -> OccupancyGrid:
    return OccupancyGrid.from_site(SITE)


def test_spec_matches_protocol(grid: OccupancyGrid) -> None:
    assert grid.spec == GridSpec(x0=-13.0, y0=-9.0, cs=0.5, w=52, h=36)
    assert SITE.staging_world[:2] == STAGING


@pytest.mark.parametrize(
    ("label", "x", "y", "expected"),
    [
        ("wall x=-9 (west)", -8.9, 0.1, BLOCKED),
        ("wall y=+6 (north)", -3.0, 6.0, BLOCKED),
        ("wall x=+9 (east)", 9.0, 2.0, BLOCKED),
        ("debris block (-7, 1)", -7.0, 1.0, BLOCKED),
        ("debris block 0.6 m high (-5, -5)", -5.0, -5.0, BLOCKED),
        ("buffer west of the x=-9 wall", -9.75, 0.25, DIFFICULT),
        ("slab interior (-5, 3)", -5.0, 3.0, DIFFICULT),
        ("V-02 under its slab", *VICTIMS["V-02"][:2], DIFFICULT),
        ("V-03 under its slab", *VICTIMS["V-03"][:2], DIFFICULT),
        ("cell west of V-01 (slab edge)", 3.25, 1.75, DIFFICULT),
        ("staging", *STAGING, PASSABLE),
        ("Go2 spawn", -11.0, -7.5, PASSABLE),
        ("open ground NE", 12.5, 8.5, PASSABLE),
    ],
)
def test_known_cells(grid: OccupancyGrid, label: str, x: float, y: float, expected: int) -> None:
    assert grid.class_at(x, y) == expected, label


def test_v01_sits_just_outside_its_slab(grid: OccupancyGrid) -> None:
    # V-01 (3.5, 2.0) is 0.2 m east of the slab's east edge: its own cell is passable and
    # the cell it touches on the west is difficult (documented in the stream E handoff).
    x, y = VICTIMS["V-01"][:2]
    assert grid.class_at(x, y) == PASSABLE
    assert grid.class_at(x - 0.25, y - 0.25) == DIFFICULT


def test_outside_grid_is_blocked(grid: OccupancyGrid) -> None:
    assert grid.class_at(20.0, 0.0) == BLOCKED
    assert grid.class_at(-13.01, 0.0) == BLOCKED


def test_class_counts_and_no_unmapped(grid: OccupancyGrid) -> None:
    counts = {k: int((grid.cls == k).sum()) for k in range(4)}
    assert counts[UNMAPPED] == 0
    assert sum(counts.values()) == 52 * 36
    assert counts[BLOCKED] > 100 and counts[DIFFICULT] > counts[BLOCKED]


def test_every_blocked_cell_has_no_passable_neighbour(grid: OccupancyGrid) -> None:
    c = grid.cls
    for j, i in zip(*np.nonzero(c == BLOCKED), strict=True):
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                nj, ni = j + dj, i + di
                if 0 <= nj < 36 and 0 <= ni < 52:
                    assert c[nj, ni] != PASSABLE


def test_cls_bytes_row_major_by_increasing_y(grid: OccupancyGrid) -> None:
    raw = grid.to_cls_bytes()
    assert len(raw) == 52 * 36
    i, j = grid.spec.cell_of(-8.9, 0.1)
    assert raw[j * 52 + i] == BLOCKED
    i, j = grid.spec.cell_of(*STAGING)
    assert raw[j * 52 + i] == PASSABLE
    snap = grid.to_snapshot()
    assert base64.b64decode(snap["cls"]) == raw
    assert {k: snap[k] for k in ("x0", "y0", "cs", "w", "h")} == {
        "x0": -13.0,
        "y0": -9.0,
        "cs": 0.5,
        "w": 52,
        "h": 36,
    }


def test_seen_mask_marks_unmapped_only_when_respected() -> None:
    seen = np.zeros((36, 52), dtype=bool)
    seen[:6, :] = True  # the southern strip y < -6
    g = OccupancyGrid.from_site(SITE, seen=seen, respect_seen=True)
    assert g.class_at(12.5, 8.5) == UNMAPPED
    assert g.class_at(*STAGING) == PASSABLE
    assert OccupancyGrid.from_site(SITE, seen=seen).class_at(12.5, 8.5) == PASSABLE
    with pytest.raises(ValueError):
        g.set_seen(np.zeros((3, 3), dtype=bool))


def test_polygons_overlap_ignores_touching() -> None:
    a = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert polygons_overlap(a, [(0.5, 0.5), (2, 0.5), (2, 2), (0.5, 2)])
    assert not polygons_overlap(a, [(1, 0), (2, 0), (2, 1), (1, 1)])


# --- planner ----------------------------------------------------------------------------


def _cells_along(route_points: list[list[float]], step: float = 0.05) -> list[tuple[float, float]]:
    out = []
    for a, b in zip(route_points, route_points[1:], strict=False):
        n = max(1, int(math.dist(a[:2], b[:2]) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]
    return out


@pytest.mark.parametrize(
    ("victim", "lo", "hi"),
    [("V-01", 20.0, 28.0), ("V-02", 10.0, 16.0), ("V-03", 17.0, 24.0)],
)
def test_routes_from_staging_reach_victims(
    grid: OccupancyGrid, victim: str, lo: float, hi: float
) -> None:
    goal = VICTIMS[victim]
    r = plan_route(grid, STAGING, goal)
    assert lo <= r.length <= hi
    assert r.points[0][:2] == list(STAGING)
    assert r.points[-1][:2] == list(goal[:2])  # goal is traversable: not snapped
    assert not r.start_snapped and not r.goal_snapped
    assert all(grid.cls[j, i] != BLOCKED for i, j in r.cells)
    assert all(grid.class_at(x, y) != BLOCKED for x, y in _cells_along(r.points))
    # straight-line lower bound, and seconds between the all-passable / all-difficult bounds
    assert r.length >= math.dist(STAGING, goal[:2])
    assert r.length / 0.6 * 0.9 <= r.seconds <= r.length / 0.3 * 1.1
    assert r.difficulty in ("easy", "moderate")


def test_route_metrics_v01(grid: OccupancyGrid) -> None:
    r = plan_route(grid, STAGING, VICTIMS["V-01"])
    assert r.climbs == 0 and r.difficulty == "easy"
    assert r.seconds == pytest.approx(r.length / 0.6, rel=0.08)
    r2 = plan_route(grid, STAGING, VICTIMS["V-02"])
    assert r2.climbs >= 1 and r2.difficulty == "moderate"


def test_planner_is_deterministic(grid: OccupancyGrid) -> None:
    for v in ("V-01", "V-02", "V-03"):
        a = plan_route(grid, STAGING, VICTIMS[v]).as_dict()
        b = plan_route(OccupancyGrid.from_site(SITE), STAGING, VICTIMS[v]).as_dict()
        assert a == b


def test_people_pay_more_for_difficult(grid: OccupancyGrid) -> None:
    go2 = plan_route(grid, STAGING, VICTIMS["V-02"], GO2)
    person = plan_route(grid, STAGING, VICTIMS["V-02"], PERSON)
    assert person.cost > go2.cost
    assert person.profile == "person" and go2.profile == "go2"


def test_goal_inside_blocked_snaps_to_nearest_free_cell(grid: OccupancyGrid) -> None:
    r = plan_route(grid, STAGING, (-7.0, 1.0, 0.0))  # inside a debris block
    assert r.goal_snapped
    end = r.points[-1]
    assert grid.class_at(end[0], end[1]) in (PASSABLE, DIFFICULT)
    assert math.dist(end[:2], (-7.0, 1.0)) < 1.5


def test_start_inside_blocked_snaps(grid: OccupancyGrid) -> None:
    r = plan_route(grid, (-8.9, 0.1), STAGING)
    assert r.start_snapped
    assert r.points[0][:2] == [-8.9, 0.1]


def test_hazard_raises_cost_and_marks_route_hard() -> None:
    clean = plan_route(OccupancyGrid.from_site(SITE), STAGING, VICTIMS["V-01"])
    hazard = (3.25, -2.0)  # on the clean route's x = 3.25 corridor
    g = OccupancyGrid.from_site(SITE, hazards=[hazard])
    r = plan_route(g, STAGING, VICTIMS["V-01"])
    assert r.cost > clean.cost
    assert r.hazards_near >= 1 and r.difficulty == "hard"


def test_unseen_cells_are_impassable() -> None:
    seen = np.zeros((36, 52), dtype=bool)
    seen[:6, :] = True
    g = OccupancyGrid.from_site(SITE, seen=seen, respect_seen=True)
    r = plan_route(g, STAGING, VICTIMS["V-01"])  # goal unmapped: snaps into the seen strip
    assert r.goal_snapped
    assert all(seen[j, i] for i, j in r.cells)


def test_no_route_when_walled_in() -> None:
    seen = np.zeros((36, 52), dtype=bool)
    seen[0:2, 0:2] = True
    seen[30:32, 40:42] = True
    g = OccupancyGrid.from_site(SITE, seen=seen, respect_seen=True)
    with pytest.raises(NoRoute):
        plan_route(g, (-12.75, -8.75), (7.25, 6.25))


def test_simplify_removes_collinear_points() -> None:
    pts = [[0, 0], [1, 0], [2, 0], [3, 1], [4, 2], [4, 3]]
    assert simplify(pts) == [[0, 0], [2, 0], [4, 2], [4, 3]]
    assert simplify([[0, 0], [1, 1]]) == [[0, 0], [1, 1]]
