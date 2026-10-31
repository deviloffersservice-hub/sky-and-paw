"""Route planner over :class:`~skypaw.mission.grid.OccupancyGrid` (SPEC §6.4).

* Dijkstra, 8-connected. Unmapped and blocked cells are impassable. A diagonal step is
  allowed only if both orthogonal cells it squeezes past are traversable (no corner cutting).
* Step cost = step length x (difficult multiplier if the cell entered is difficult: x2.2 Go2,
  x3 people) x (x4 if the cell entered is within 1.2 m of a hazard).
* Start and goal snap to the nearest traversable cell when theirs is not (a goal under a
  slab is difficult, hence traversable, and is used as is).
* The cell path is simplified by removing collinear interior points; the route starts at the
  given start position and ends at the goal (or the snapped goal cell's centre).
* Metrics: length, climbs (entries into difficult runs), seconds (passable / difficult speeds
  per profile, Go2 0.6 / 0.3 m/s), hazards within 1.8 m, difficulty (hard if any hazard or
  >= 3 climbs; moderate if >= 1 climb; else easy).

Deterministic: ties in the priority queue break on the cell index.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from skypaw.mission.grid import DIFFICULT, OccupancyGrid

HAZARD_COST_RADIUS_M = 1.2
HAZARD_COST_MULT = 4.0
HAZARD_REPORT_RADIUS_M = 1.8
_SQRT2 = math.sqrt(2.0)
_NEIGHBOURS = (
    (1, 0, 1.0),
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (0, -1, 1.0),
    (1, 1, _SQRT2),
    (1, -1, _SQRT2),
    (-1, 1, _SQRT2),
    (-1, -1, _SQRT2),
)


class NoRoute(RuntimeError):
    """No traversable route connects start and goal."""


@dataclass(frozen=True)
class RobotProfile:
    """Routing cost and speed parameters for one kind of mover (SPEC §6.4)."""

    name: str
    difficult_mult: float
    v_passable: float
    v_difficult: float


GO2 = RobotProfile("go2", difficult_mult=2.2, v_passable=0.6, v_difficult=0.3)
PERSON = RobotProfile("person", difficult_mult=3.0, v_passable=0.7, v_difficult=0.25)


@dataclass
class Route:
    """A planned route in the world frame (points are ``[x, y, z]``, z = ground)."""

    points: list[list[float]]
    length: float
    climbs: int
    seconds: float
    difficulty: str
    hazards_near: int = 0
    cost: float = 0.0
    cells: list[tuple[int, int]] = field(default_factory=list)
    profile: str = GO2.name
    start_snapped: bool = False
    goal_snapped: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form (rounded)."""
        return {
            "profile": self.profile,
            "points": [[round(v, 3) for v in p] for p in self.points],
            "length": round(self.length, 2),
            "climbs": self.climbs,
            "seconds": round(self.seconds, 1),
            "difficulty": self.difficulty,
            "hazards_near": self.hazards_near,
            "cost": round(self.cost, 3),
            "cells": [list(c) for c in self.cells],
            "start_snapped": self.start_snapped,
            "goal_snapped": self.goal_snapped,
        }


def _hazard_mask(grid: OccupancyGrid, radius: float) -> list[list[bool]]:
    s = grid.spec
    out = [[False] * s.w for _ in range(s.h)]
    if not grid.hazards:
        return out
    for j in range(s.h):
        for i in range(s.w):
            cx, cy = s.centre(i, j)
            out[j][i] = any(math.hypot(cx - hx, cy - hy) <= radius for hx, hy in grid.hazards)
    return out


def snap_to_traversable(grid: OccupancyGrid, x: float, y: float) -> tuple[int, int]:
    """The traversable cell nearest to world ``(x, y)`` (by centre distance, then index)."""
    s = grid.spec
    ok = grid.traversable_mask()
    i, j = s.cell_of(x, y)
    if s.in_bounds(i, j) and ok[j, i]:
        return (i, j)
    best: tuple[float, int, int] | None = None
    for jj in range(s.h):
        for ii in range(s.w):
            if ok[jj, ii]:
                cx, cy = s.centre(ii, jj)
                cand = (math.hypot(cx - x, cy - y), jj, ii)
                if best is None or cand < best:
                    best = cand
    if best is None:
        raise NoRoute("the grid has no traversable cell")
    return (best[2], best[1])


def _dijkstra(
    grid: OccupancyGrid,
    start: tuple[int, int],
    goal: tuple[int, int],
    profile: RobotProfile,
    hazard: list[list[bool]],
) -> tuple[list[tuple[int, int]], float]:
    s = grid.spec
    ok = grid.traversable_mask()
    cls = grid.cls
    w = s.w

    def idx(c: tuple[int, int]) -> int:
        return c[1] * w + c[0]

    dist: dict[int, float] = {idx(start): 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, idx(start))]
    goal_k = idx(goal)
    while heap:
        d, k = heapq.heappop(heap)
        if d > dist.get(k, math.inf):
            continue
        if k == goal_k:
            break
        i, j = k % w, k // w
        for di, dj, step in _NEIGHBOURS:
            ni, nj = i + di, j + dj
            if not s.in_bounds(ni, nj) or not ok[nj, ni]:
                continue
            if di and dj and not (ok[j, ni] and ok[nj, i]):
                continue
            mult = profile.difficult_mult if cls[nj, ni] == DIFFICULT else 1.0
            if hazard[nj][ni]:
                mult *= HAZARD_COST_MULT
            nd = d + step * s.cs * mult
            nk = nj * w + ni
            if nd < dist.get(nk, math.inf) - 1e-12:
                dist[nk] = nd
                prev[nk] = k
                heapq.heappush(heap, (nd, nk))
    if goal_k not in dist:
        raise NoRoute(f"no route from cell {start} to cell {goal}")
    path = [goal_k]
    while path[-1] != idx(start):
        path.append(prev[path[-1]])
    path.reverse()
    return [(k % w, k // w) for k in path], dist[goal_k]


def simplify(points: Sequence[Sequence[float]], tol: float = 1e-6) -> list[list[float]]:
    """Drop interior points that lie on the straight line through their neighbours."""
    pts = [list(p) for p in points]
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for k in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[k], pts[k + 1]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        dot = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
        if abs(cross) > tol or dot < 0:
            out.append(b)
    out.append(pts[-1])
    return out


def _difficulty(climbs: int, hazards_near: int) -> str:
    if hazards_near > 0 or climbs >= 3:
        return "hard"
    if climbs >= 1:
        return "moderate"
    return "easy"


def plan_route(
    grid: OccupancyGrid,
    start: Sequence[float],
    goal: Sequence[float],
    profile: RobotProfile = GO2,
) -> Route:
    """Plan a route from world ``start`` to world ``goal`` (``[x, y]`` or ``[x, y, z]``).

    Raises :class:`NoRoute` if the snapped start and goal are not connected.
    """
    s = grid.spec
    sx, sy = float(start[0]), float(start[1])
    gx, gy = float(goal[0]), float(goal[1])
    start_c = snap_to_traversable(grid, sx, sy)
    goal_c = snap_to_traversable(grid, gx, gy)
    start_snapped = start_c != s.cell_of(sx, sy)
    goal_snapped = goal_c != s.cell_of(gx, gy)
    hazard = _hazard_mask(grid, HAZARD_COST_RADIUS_M)
    cells, cost = _dijkstra(grid, start_c, goal_c, profile, hazard)

    cls = grid.cls
    centres = [s.centre(i, j) for i, j in cells]
    first = (sx, sy) if not start_snapped else centres[0]
    last = (gx, gy) if not goal_snapped else centres[-1]
    # Cell centres between the true endpoints: drop the endpoint cells' own centres.
    raw: list[tuple[float, float]] = [first, *centres[1:-1], last]
    if start_snapped:
        raw.insert(0, (sx, sy))
    points2d = simplify(raw)
    length = sum(math.dist(points2d[k], points2d[k + 1]) for k in range(len(points2d) - 1))

    climbs = 0
    prev_diff = False
    seconds = 0.0
    for k, (i, j) in enumerate(cells):
        is_diff = cls[j, i] == DIFFICULT
        if is_diff and not prev_diff:
            climbs += 1
        prev_diff = is_diff
        if k:
            pi, pj = cells[k - 1]
            d = math.dist(s.centre(pi, pj), s.centre(i, j))
            v_prev = profile.v_difficult if cls[pj, pi] == DIFFICULT else profile.v_passable
            v_here = profile.v_difficult if is_diff else profile.v_passable
            seconds += d / 2 / v_prev + d / 2 / v_here
    if len(cells) == 1:
        v = (
            profile.v_difficult
            if cls[cells[0][1], cells[0][0]] == DIFFICULT
            else profile.v_passable
        )
        seconds = length / v

    hazards_near = sum(
        1
        for hx, hy in grid.hazards
        if any(math.hypot(cx - hx, cy - hy) <= HAZARD_REPORT_RADIUS_M for cx, cy in centres)
    )
    return Route(
        points=[[float(x), float(y), 0.0] for x, y in points2d],
        length=length,
        climbs=climbs,
        seconds=seconds,
        difficulty=_difficulty(climbs, hazards_near),
        hazards_near=hazards_near,
        cost=cost,
        cells=cells,
        profile=profile.name,
        start_snapped=start_snapped,
        goal_snapped=goal_snapped,
    )


__all__ = [
    "GO2",
    "HAZARD_COST_MULT",
    "HAZARD_COST_RADIUS_M",
    "HAZARD_REPORT_RADIUS_M",
    "PERSON",
    "NoRoute",
    "RobotProfile",
    "Route",
    "plan_route",
    "simplify",
    "snap_to_traversable",
]
