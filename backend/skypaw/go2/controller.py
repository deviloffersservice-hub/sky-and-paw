"""Go2 rescue controller: grid route + closed-loop velocity bursts (implements ``Go2Executor``).

Why bursts: in an unmapped MuJoCo scene ``twin.navigation.goto`` / ``follow_path`` stay
``queued`` forever, while ``twin.locomotion.move_forward(speed, duration=)`` and
``turn_left/turn_right(rate, duration=)`` do move the Go2 (RESEARCH §6, day-one round 2,
D-R2-6). There is no obstacle avoidance on that path, so the route comes from our planner.

Loop (one decision per burst, pose from MQTT ``position`` / ``rotation`` at ~8 Hz):

1. read the pose; abort ``stale_pose`` if it is older than ``stale_after_s`` (3 s);
2. advance past waypoints within ``waypoint_tol_m`` or already passed along the segment;
3. at the last point, stop as ``arrived`` within ``arrive_tol_m`` (0.7 m);
4. heading error > ``big_turn_rad`` (0.6): ``turn_left``/``turn_right(0.8, duration=0.5)``;
   >= ``heading_tol_rad`` (0.2): ``turn_*(0.6, duration=0.3)``; else
   ``move_forward(0.4, duration=0.5)``. The achieved yaw rate of every turn burst is logged
   (``go2_turn_rate``) and kept in the trace: live run 3 measured ~0.07 rad/s for 0.6 commanded;
   Turns use the named ``turn_left`` / ``turn_right`` commands by default: live runs 1-3
   measured them turning CCW / CW respectively (the sim reads the command name; the SDK's
   ``angular_z`` is ``+abs(rate)`` for both). ``turn_api="move"`` sends
   ``locomotion.move(angular_z=+-rate)`` instead, but cyberwave 0.7.4 refuses it client-side
   for the Go2 (run 4: "Command 'move' is not in the MQTT catalog for this twin"), so that
   error falls back to the named commands. The first turn burst self-checks the direction
   (sign flip for ``move``; logged only for named) as ``go2_turn_calibration``;
5. abort ``timeout`` after ``timeout_s`` (default from the route:
   ``max(120, 2.5 * route.seconds + 30)`` capped at 300 s), and ``stuck`` when over the last
   ``stuck_window`` bursts the Go2 neither moved ``stuck_min_m`` nor turned ``stuck_min_yaw``
   (pushing against rubble: there is no obstacle avoidance on the velocity path).

Every burst blocks for its duration inside the SDK (it publishes at 20 Hz then ``stop``),
so bursts run in a worker thread. Progress callbacks carry the snapshot fields stream A
publishes as ``robots.go2`` (``pos``, ``yaw``, ``mode``, ``target``, ``path``, ``pose_age_s``).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from skypaw.logging import get_logger, log_event
from skypaw.mission.grid import OccupancyGrid
from skypaw.mission.planner import GO2, NoRoute, RobotProfile, Route, plan_route
from skypaw.sim.client import assert_client_simulation
from skypaw.sim.guard import assert_simulation_only

log = get_logger(__name__)

#: Terminal statuses of a goto. ``arrived`` is the only success.
STATUSES = ("arrived", "stale_pose", "timeout", "stuck", "stopped", "no_route", "error")


@dataclass(frozen=True)
class PoseReading:
    """One Go2 pose: world position (m), yaw about +Z (rad), receive time ``t`` (clock s)."""

    x: float
    y: float
    z: float
    yaw: float
    t: float


class PoseSource(Protocol):
    """Anything that returns the latest Go2 pose (or ``None`` before the first message)."""

    def latest(self) -> PoseReading | None: ...


def yaw_of(q: dict[str, float]) -> float:
    """Yaw (rad, about world +Z) of a quaternion ``{x, y, z, w}``."""
    x, y, z, w = (float(q.get(k, 0.0)) for k in "xyzw")
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def wrap(angle: float) -> float:
    """Angle wrapped to [-pi, pi)."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


class CachePoseSource:
    """Adapter over ``skypaw.sim.mqtt_ws.PoseCache`` (read-only; the cache is not modified).

    The cache stores ``position_t`` / ``rotation_t`` as epoch seconds (``time.time()``);
    the reading's ``t`` is the older of the two, so a stale rotation counts as stale.
    """

    def __init__(self, cache: Any, twin_uuid: str) -> None:
        self._cache = cache
        self._uuid = twin_uuid

    def latest(self) -> PoseReading | None:
        """The latest pose, or ``None`` until both position and rotation have arrived."""
        entry = self._cache.latest(self._uuid)
        if not entry or "position" not in entry or "rotation" not in entry:
            return None
        p = entry["position"]
        t = min(float(entry.get("position_t", 0.0)), float(entry.get("rotation_t", 0.0)))
        return PoseReading(
            float(p.get("x", 0.0)),
            float(p.get("y", 0.0)),
            float(p.get("z", 0.0)),
            yaw_of(entry["rotation"]),
            t,
        )


@dataclass(frozen=True)
class ControllerConfig:
    """Tunables (defaults are the v1 values from the task / SPEC §6.4)."""

    turn_rate: float = 0.6  # rad/s
    turn_burst_s: float = 0.3
    big_turn_rad: float = 0.6  # above this heading error use the big turn burst
    big_turn_rate: float = 0.8
    big_turn_burst_s: float = 0.5
    turn_api: str = "named"  # "named" (turn_left / turn_right) or "move" (signed angular_z)
    turn_check_min_rad: float = 0.02  # first-burst yaw change below this = no effect
    forward_speed: float = 0.4  # m/s
    forward_burst_s: float = 0.5
    heading_tol_rad: float = 0.2
    waypoint_tol_m: float = 0.35
    arrive_tol_m: float = 0.7
    stale_after_s: float = 3.0
    timeout_s: float | None = None  # None: from the route, see goto_timeout()
    timeout_min_s: float = 120.0
    timeout_max_s: float = 300.0
    stuck_window: int = 12  # bursts of any kind
    stuck_min_m: float = 0.15
    stuck_min_yaw: float = 0.1  # rad, summed |yaw change| over the window
    poll_s: float = 0.1


def goto_timeout(cfg: ControllerConfig, route_seconds: float) -> float:
    """``cfg.timeout_s`` if set, else ``max(120, 2.5 * route_seconds + 30)`` capped at 300."""
    if cfg.timeout_s is not None:
        return float(cfg.timeout_s)
    return min(cfg.timeout_max_s, max(cfg.timeout_min_s, 2.5 * route_seconds + 30.0))


@dataclass
class Go2Progress:
    """One progress update (``robots.go2`` fields for the snapshot plus extras)."""

    t: float  # seconds since the goto started
    mode: str  # idle | moving | arrived | blocked
    target: str | None
    pos: list[float] | None
    yaw: float | None
    path: list[list[float]]  # current position, then the remaining route points
    waypoint_idx: int
    dist_remaining: float | None
    pose_age_s: float | None
    action: str = ""

    def snapshot_fields(self) -> dict[str, Any]:
        """The PROTOCOL ``robots.go2`` keys this controller owns."""
        return {
            "pos": self.pos,
            "yaw": self.yaw,
            "mode": self.mode,
            "target": self.target,
            "path": self.path,
            "pose_age_s": self.pose_age_s,
        }


@dataclass
class GotoResult:
    """Outcome of one :meth:`Go2Controller.navigate` call."""

    status: str
    target_id: str | None
    goal: list[float]
    route: Route | None
    started_t: float
    ended_t: float
    final_pose: PoseReading | None = None
    distance_error_m: float | None = None  # final pose to the requested goal
    route_end_error_m: float | None = None  # final pose to the route's last point
    turns: int = 0
    forwards: int = 0
    timeout_s: float | None = None
    turn_rates: list[dict[str, float]] = field(default_factory=list)
    turn_calibration: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def arrived(self) -> bool:
        """True if the Go2 reached the goal tolerance."""
        return self.status == "arrived"

    @property
    def seconds(self) -> float:
        """Time from the goto call to its end (time-to-arrival when arrived)."""
        return self.ended_t - self.started_t

    def as_dict(self, *, with_trace: bool = False) -> dict[str, Any]:
        """JSON-friendly summary."""
        fp = self.final_pose
        out: dict[str, Any] = {
            "status": self.status,
            "target_id": self.target_id,
            "goal": self.goal,
            "seconds": round(self.seconds, 2),
            "final_pose": (
                None
                if fp is None
                else {
                    "x": round(fp.x, 3),
                    "y": round(fp.y, 3),
                    "z": round(fp.z, 3),
                    "yaw": round(fp.yaw, 3),
                }
            ),
            "distance_error_m": _r(self.distance_error_m),
            "route_end_error_m": _r(self.route_end_error_m),
            "turns": self.turns,
            "forwards": self.forwards,
            "timeout_s": self.timeout_s,
            "turn_rate": _turn_rate_summary(self.turn_rates),
            "turn_calibration": self.turn_calibration,
            "route": self.route.as_dict() if self.route else None,
            "error": self.error,
        }
        if with_trace:
            out["trace"] = self.trace
        return out


def _no_progress(window: list[tuple[float, float, float]], min_m: float, min_yaw: float) -> bool:
    """True if the poses in ``window`` show neither translation nor rotation."""
    moved = math.dist(window[0][:2], window[-1][:2])
    turned = sum(abs(wrap(b[2] - a[2])) for a, b in zip(window, window[1:], strict=False))
    return moved < min_m and turned < min_yaw


def _turn_rate_summary(rates: list[dict[str, float]]) -> dict[str, Any] | None:
    """Mean achieved |yaw rate| per commanded rate over the turn bursts."""
    if not rates:
        return None
    out: dict[str, Any] = {}
    for cmd in sorted({r["cmd"] for r in rates}):
        got = [r["achieved"] for r in rates if r["cmd"] == cmd]
        out[f"{cmd:g}"] = {"bursts": len(got), "achieved_mean": round(sum(got) / len(got), 3)}
    return out


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


ProgressCallback = Callable[[Go2Progress], None]
BlockingRunner = Callable[..., Awaitable[Any]]


class Go2Controller:
    """``ports.Go2Executor`` for the simulated Go2 (see the module docstring)."""

    def __init__(
        self,
        twin: Any,
        pose_source: PoseSource,
        *,
        grid: OccupancyGrid | None = None,
        profile: RobotProfile = GO2,
        config: ControllerConfig | None = None,
        on_progress: ProgressCallback | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        run_blocking: BlockingRunner = asyncio.to_thread,
    ) -> None:
        self.twin = twin
        self.pose_source = pose_source
        self.grid = grid if grid is not None else OccupancyGrid.from_site()
        self.profile = profile
        self.config = config or ControllerConfig()
        self.on_progress = on_progress
        self._clock = clock
        self._sleep = sleep
        self._run = run_blocking
        self._stop_requested = False
        self.mode = "idle"
        self._turn_api = self.config.turn_api
        self._turn_sign = 1.0  # multiplies angular_z (+ = CCW)
        self._turn_checked = False
        self.turn_calibration: dict[str, Any] | None = None
        self.target: str | None = None
        self.last_result: GotoResult | None = None

    # ports.Go2Executor ------------------------------------------------------------------
    async def goto(self, position: list[float], target_id: str | None) -> None:
        """Plan to ``position`` (world) and drive there; the outcome is in ``last_result``."""
        await self.navigate(position, target_id)

    async def stop(self) -> None:
        """Interrupt any goto after the current burst and send ``locomotion.stop()``."""
        self._stop_requested = True
        log_event(log, "go2_stop_requested", target=self.target)
        await self._run(self.twin.locomotion.stop)

    # main loop -------------------------------------------------------------------------
    async def navigate(self, position: Sequence[float], target_id: str | None) -> GotoResult:
        """Plan and follow a route to ``position``; returns the :class:`GotoResult`."""
        cfg = self.config
        self._assert_simulation()
        self._stop_requested = False
        self.target = target_id
        goal = [float(v) for v in position[:3]] + [0.0] * max(0, 3 - len(position))
        t0 = self._clock()
        result = GotoResult("error", target_id, goal, None, t0, t0)
        self.last_result = result
        try:
            pose = await self._wait_pose(t0)
            if pose is None:
                return self._finish(result, "stale_pose", None, error="no Go2 pose")
            try:
                route = plan_route(self.grid, (pose.x, pose.y), goal, self.profile)
            except NoRoute as exc:
                return self._finish(result, "no_route", pose, error=str(exc))
            result.route = route
            timeout_s = result.timeout_s = goto_timeout(cfg, route.seconds)
            log_event(
                log,
                "go2_route",
                target=target_id,
                length=round(route.length, 2),
                points=len(route.points),
                est_s=round(route.seconds, 1),
                difficulty=route.difficulty,
                timeout_s=round(timeout_s, 1),
            )
            pts = route.points
            k = 1 if len(pts) > 1 else 0
            window: list[tuple[float, float, float]] = []
            self.mode = "moving"
            while True:
                now = self._clock()
                pose = self.pose_source.latest()
                age = None if pose is None else now - pose.t
                if self._stop_requested:
                    return self._finish(result, "stopped", pose)
                if pose is None or age is None or age > cfg.stale_after_s:
                    return self._finish(result, "stale_pose", pose, error=f"pose age {age}")
                if now - t0 > timeout_s:
                    return self._finish(result, "timeout", pose)
                k = self._advance(pts, k, pose)
                last = k == len(pts) - 1
                tx, ty = pts[k][0], pts[k][1]
                dist = math.hypot(tx - pose.x, ty - pose.y)
                if last and dist <= cfg.arrive_tol_m:
                    return self._finish(result, "arrived", pose)
                err = wrap(math.atan2(ty - pose.y, tx - pose.x) - pose.yaw)
                if abs(err) >= cfg.heading_tol_rad:
                    action = "turn_left" if err > 0 else "turn_right"
                else:
                    action = "forward"
                self._emit(now - t0, pose, pts, k, age, action)
                result.trace.append(
                    {
                        "t": round(now - t0, 3),
                        "x": round(pose.x, 3),
                        "y": round(pose.y, 3),
                        "yaw": round(pose.yaw, 3),
                        "wp": k,
                        "dist": round(dist, 3),
                        "err": round(err, 3),
                        "action": action,
                        "age": round(age, 3),
                    }
                )
                window.append((pose.x, pose.y, pose.yaw))
                if len(window) > cfg.stuck_window:
                    window.pop(0)
                    if _no_progress(window, cfg.stuck_min_m, cfg.stuck_min_yaw):
                        return self._finish(result, "stuck", pose)
                if action == "forward":
                    result.forwards += 1
                    await self._run(
                        self.twin.locomotion.move_forward,
                        cfg.forward_speed,
                        duration=cfg.forward_burst_s,
                    )
                else:
                    result.turns += 1
                    big = abs(err) > cfg.big_turn_rad
                    rate = cfg.big_turn_rate if big else cfg.turn_rate
                    dur = cfg.big_turn_burst_s if big else cfg.turn_burst_s
                    await self._turn(action, rate, dur)
                    self._record_turn(result, pose, rate, dur)
                    if not self._turn_checked:
                        self._check_turn(result, pose, action)
        except Exception as exc:  # noqa: BLE001 - report, stop the robot, never re-raise silently
            log.exception("go2_goto_error")
            return self._finish(result, "error", self.pose_source.latest(), error=repr(exc))
        finally:
            if result.status != "stopped":
                try:
                    await self._run(self.twin.locomotion.stop)
                except Exception as exc:  # noqa: BLE001
                    log_event(log, "go2_stop_error", level=30, error=repr(exc))

    def _assert_simulation(self) -> None:
        """Refuse to move unless configured and SDK runtime modes are simulation.

        An SDK twin carries its client (``twin.client``); its ``config.runtime_mode`` is
        what ``simulation_affect`` set. Fakes without a client are checked on settings only.
        """
        assert_simulation_only()
        client = getattr(self.twin, "client", None)
        if client is not None:
            assert_client_simulation(client)

    async def _turn(self, action: str, rate: float, dur: float) -> None:
        """One turn burst toward ``action`` (``turn_left`` = CCW) with the current API."""
        if self._turn_api == "move":
            sign = 1.0 if action == "turn_left" else -1.0
            try:
                await self._run(
                    self.twin.locomotion.move,
                    linear_x=0.0,
                    angular_z=sign * self._turn_sign * abs(rate),
                    duration=dur,
                )
                return
            except ValueError as exc:  # SDK catalog check, raised before anything is published
                if "not in the MQTT catalog" not in str(exc):
                    raise
                self._turn_api = "named"
                log_event(
                    log,
                    "go2_turn_calibration",
                    level=30,
                    api="move",
                    verdict="move_not_in_catalog_fallback_named",
                    error=str(exc)[:200],
                )
        await self._run(getattr(self.twin.locomotion, action), abs(rate), duration=dur)

    def _check_turn(self, result: GotoResult, before: PoseReading, action: str) -> None:
        """After the first turn burst: keep, flip the sign, or fall back to named turns."""
        after = self.pose_source.latest()
        if after is None or after.t <= before.t:
            return  # no fresh pose yet: check on the next burst
        self._turn_checked = True
        dyaw = wrap(after.yaw - before.yaw)
        want = 1.0 if action == "turn_left" else -1.0
        api, sign = self._turn_api, self._turn_sign
        if abs(dyaw) < self.config.turn_check_min_rad:
            verdict = "no_effect_fallback_named" if api == "move" else "no_effect"
            if api == "move":
                self._turn_api = "named"
        elif dyaw * want > 0:
            verdict = "sign_ok"
        else:
            verdict = "sign_flipped" if api == "move" else "named_wrong_direction"
            if api == "move":
                self._turn_sign = -sign
        self.turn_calibration = {
            "api": api,
            "angular_z_sign": sign,
            "requested": action,
            "dyaw": round(dyaw, 4),
            "verdict": verdict,
            "now_api": self._turn_api,
            "now_sign": self._turn_sign,
        }
        result.turn_calibration = self.turn_calibration
        log_event(log, "go2_turn_calibration", **self.turn_calibration)

    def _record_turn(
        self, result: GotoResult, before: PoseReading, rate: float, dur: float
    ) -> None:
        """Achieved |yaw rate| between the pose before the burst and the latest pose."""
        after = self.pose_source.latest()
        if after is None or after.t <= before.t:
            return
        achieved = abs(wrap(after.yaw - before.yaw)) / (after.t - before.t)
        rec = {"cmd": rate, "dur": dur, "achieved": round(achieved, 3)}
        result.turn_rates.append(rec)
        if result.trace:
            result.trace[-1]["yaw_rate"] = rec["achieved"]
        log_event(log, "go2_turn_rate", commanded=rate, duration=dur, achieved=rec["achieved"])

    async def _wait_pose(self, t0: float) -> PoseReading | None:
        """First fresh pose, waiting up to ``stale_after_s``."""
        cfg = self.config
        while True:
            pose = self.pose_source.latest()
            now = self._clock()
            if pose is not None and now - pose.t <= cfg.stale_after_s:
                return pose
            if now - t0 > cfg.stale_after_s:
                return None
            await self._sleep(cfg.poll_s)

    def _advance(self, pts: list[list[float]], k: int, pose: PoseReading) -> int:
        """Skip waypoints that are within tolerance or already passed (never the last)."""
        while k < len(pts) - 1:
            tx, ty = pts[k][0], pts[k][1]
            if math.hypot(tx - pose.x, ty - pose.y) <= self.config.waypoint_tol_m:
                k += 1
                continue
            px, py = pts[k - 1][0], pts[k - 1][1]
            sx, sy = tx - px, ty - py
            seg2 = sx * sx + sy * sy
            if seg2 > 0 and ((pose.x - px) * sx + (pose.y - py) * sy) / seg2 >= 1.0:
                k += 1
                continue
            break
        return k

    def _emit(
        self,
        t: float,
        pose: PoseReading | None,
        pts: list[list[float]],
        k: int,
        age: float | None,
        action: str,
    ) -> None:
        if self.on_progress is None:
            return
        pos = None if pose is None else [pose.x, pose.y, pose.z]
        remaining = [list(p) for p in pts[k:]] if pts else []
        path = ([[pose.x, pose.y, 0.0]] if pose is not None else []) + remaining
        dist = (
            sum(math.dist(path[n][:2], path[n + 1][:2]) for n in range(len(path) - 1))
            if path
            else None
        )
        prog = Go2Progress(
            t=t,
            mode=self.mode,
            target=self.target,
            pos=pos,
            yaw=None if pose is None else pose.yaw,
            path=path,
            waypoint_idx=k,
            dist_remaining=dist,
            pose_age_s=age,
            action=action,
        )
        try:
            self.on_progress(prog)
        except Exception:  # noqa: BLE001 - a bad listener must not stop the robot
            log.exception("go2_progress_callback_error")

    def _finish(
        self, result: GotoResult, status: str, pose: PoseReading | None, *, error: str | None = None
    ) -> GotoResult:
        result.status = status
        result.ended_t = self._clock()
        result.final_pose = pose
        result.error = error
        if pose is not None:
            result.distance_error_m = math.hypot(result.goal[0] - pose.x, result.goal[1] - pose.y)
            if result.route is not None:
                end = result.route.points[-1]
                result.route_end_error_m = math.hypot(end[0] - pose.x, end[1] - pose.y)
        self.mode = {"arrived": "arrived", "stopped": "idle"}.get(status, "blocked")
        pts = result.route.points if result.route else []
        self._emit(result.seconds, pose, pts, len(pts) - 1 if pts else 0, None, status)
        log_event(
            log,
            "go2_goto_end",
            level=20 if status in ("arrived", "stopped") else 30,
            status=status,
            target=result.target_id,
            seconds=round(result.seconds, 2),
            distance_error_m=_r(result.distance_error_m),
            turns=result.turns,
            forwards=result.forwards,
            turn_rate=_turn_rate_summary(result.turn_rates),
            error=error,
        )
        return result


__all__ = [
    "STATUSES",
    "goto_timeout",
    "CachePoseSource",
    "ControllerConfig",
    "Go2Controller",
    "Go2Progress",
    "GotoResult",
    "PoseReading",
    "PoseSource",
    "wrap",
    "yaw_of",
]
