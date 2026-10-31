"""SurveyDroneExecutor: the real ``ports.DroneExecutor`` (takeoff, lawnmower survey, hold, land).

``start_survey()``: take off to 6 m -> fly the 10 lawnmower corners of ``site.yaml`` in order
(closed loop on MQTT pose: turn toward the target until the heading error is < 0.25 rad,
then 1 s ``move_forward`` bursts; tolerance 0.5 m) -> capture a downward frame at least
every 2.5 m of travel -> reveal coverage within 2.8 m of every pose check -> fly to the
hold point and hold. Safety: it never commands a burst whose end point leaves the geofence
(x ∈ [-12, 12], y ∈ [-10, 10], z ∈ [0, 8]); it aborts to *hold* (stop sending, ``stop``)
when the pose is older than 3 s or outside the fence. ``hold()`` and ``land()`` interrupt a
running survey between bursts.

Hold point: PROTOCOL/task say (0, 0, 9) world, but z = 9 is outside the z ≤ 8 fence and no
altitude-change command has been verified, so the drone holds at (0, 0) at survey altitude.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from skypaw.drone.io import (
    BOTTOM_CAMERA,
    FlightControls,
    FrameGrabber,
    Pose,
    PoseSource,
    camera_intrinsics,
    wrap,
)
from skypaw.logging import get_logger, log_event
from skypaw.mission.ports import Frame
from skypaw.scene.site import Site, load_site, site_to_world

log = get_logger(__name__)


@dataclass(frozen=True)
class Fence:
    """Axis-aligned world-frame geofence."""

    x: tuple[float, float] = (-12.0, 12.0)
    y: tuple[float, float] = (-10.0, 10.0)
    z: tuple[float, float] = (0.0, 8.0)

    def contains(self, x: float, y: float, z: float | None = None) -> bool:
        """True if the point is inside (z ignored when ``None``)."""
        ok = self.x[0] <= x <= self.x[1] and self.y[0] <= y <= self.y[1]
        return ok and (z is None or self.z[0] <= z <= self.z[1])

    def clamp(self, x: float, y: float, margin: float = 0.5) -> tuple[float, float]:
        """Clamp a target into the fence with ``margin`` m to spare."""
        return (
            min(max(x, self.x[0] + margin), self.x[1] - margin),
            min(max(y, self.y[0] + margin), self.y[1] - margin),
        )


@dataclass
class DroneConfig:
    """Survey parameters (SPEC §6.3, M2 task, v1 task)."""

    alt: float = 6.0
    speed: float = 2.2
    min_speed: float = 0.3
    #: 0.85 s at 2.2 m/s = 1.87 m commanded; live run 2 saw bursts carry up to 1.28x the
    #: commanded distance (momentum), so one burst stays under the 2.5 m capture spacing.
    burst_s: float = 0.85
    #: Capture before a forward burst once this much travel accumulated since the last frame.
    capture_min_m: float = 0.2
    tol: float = 0.5
    heading_tol: float = 0.25
    #: Wider heading tolerance while far from the target (pure pursuit re-aims every burst);
    #: live run 1 spent most of its time in small heading corrections at 0.25 rad.
    heading_tol_far: float = 0.45
    far_m: float = 2.5
    turn_rate: float = 1.0
    min_turn_s: float = 0.3
    capture_spacing: float = 2.5
    reveal_r: float = 2.8
    stale_s: float = 3.0
    takeoff_timeout_s: float = 20.0
    takeoff_retry_s: float = 8.0
    leg_timeout_s: float = 60.0
    land_timeout_s: float = 25.0
    hold_xy: tuple[float, float] = (0.0, 0.0)
    fence: Fence = field(default_factory=Fence)
    poll_s: float = 0.1

    @property
    def max_burst_m(self) -> float:
        """Longest distance one forward burst is commanded to cover."""
        return self.speed * self.burst_s


class DroneSink(Protocol):
    """Where the executor reports (the mission runtime implements it)."""

    def drone_mode(self, mode: str) -> None: ...
    def drone_waypoint(self, idx: int) -> None: ...
    def drone_reveal(self, x: float, y: float, r: float) -> None: ...
    def next_frame_id(self) -> str: ...
    async def drone_frame(self, frame: Frame) -> None: ...
    def drone_log(self, key: str, text: str, level: str = "info") -> None: ...


class AbortToHold(RuntimeError):
    """Raised inside the survey when it must stop and hold."""


def survey_corners(site: Site) -> list[tuple[float, float, float]]:
    """The 10 lawnmower corners from ``site.yaml`` in flight order, world frame."""
    return [site_to_world(*c) for c in site.survey.corners()]


@dataclass
class SurveyReport:
    """Outcome of the last survey (also in the log)."""

    corners_total: int = 0
    corners_reached: list[int] = field(default_factory=list)
    frames: int = 0
    max_capture_gap_m: float = 0.0
    path_m: float = 0.0
    duration_s: float = 0.0
    aborted: str | None = None
    takeoff_s: float | None = None
    held: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "corners_total": self.corners_total,
            "corners_reached": list(self.corners_reached),
            "n_corners_reached": len(self.corners_reached),
            "frames": self.frames,
            "max_capture_gap_m": round(self.max_capture_gap_m, 2),
            "path_m": round(self.path_m, 1),
            "duration_s": round(self.duration_s, 1),
            "aborted": self.aborted,
            "takeoff_s": self.takeoff_s,
            "held": self.held,
        }


class SurveyDroneExecutor:
    """Implements ``ports.DroneExecutor`` on top of pose / controls / camera adapters."""

    def __init__(
        self,
        controls: FlightControls,
        pose: PoseSource,
        camera: FrameGrabber,
        sink: DroneSink,
        frames_dir: Path,
        *,
        site: Site | None = None,
        config: DroneConfig | None = None,
        clock: Callable[[], float] = time.time,
        sensor_id: str = BOTTOM_CAMERA,
        mission_t: Callable[[], float] | None = None,
    ) -> None:
        self.controls = controls
        self.pose = pose
        self.camera = camera
        self.sink = sink
        self.frames_dir = Path(frames_dir)
        self.site = site or load_site()
        self.cfg = config or DroneConfig()
        self.clock = clock
        self.sensor_id = sensor_id
        self.mission_t = mission_t or (lambda: 0.0)
        self.corners = survey_corners(self.site)
        self.report = SurveyReport(corners_total=len(self.corners))
        self._abort: str | None = None
        self._busy = asyncio.Lock()
        self._last_pose: Pose | None = None
        self._since_capture = 0.0
        self._intrinsics = camera_intrinsics()

    # --- public port ----------------------------------------------------------------
    @property
    def busy(self) -> bool:
        """A survey (or landing) is running."""
        return self._busy.locked()

    async def start_survey(self) -> None:
        """Take off, fly the lawnmower with captures, then hold. Returns when done/aborted."""
        if self._busy.locked():
            raise RuntimeError("drone is busy")
        async with self._busy:
            self._abort = None
            self.report = SurveyReport(corners_total=len(self.corners))
            t0 = self.clock()
            try:
                await self._takeoff()
                self.sink.drone_mode("survey")
                self._since_capture = 0.0
                await self._capture(self._fresh_pose())
                for idx, (x, y, _z) in enumerate(self.corners):
                    self.sink.drone_waypoint(idx)
                    reached = await self._fly_to(x, y, capture=True)
                    if reached:
                        self.report.corners_reached.append(idx)
                        self.sink.drone_log("corner", f"Survey corner {idx + 1}/10 reached")
                    else:
                        self.sink.drone_log(
                            "corner_timeout", f"Corner {idx + 1}/10 not reached", "warn"
                        )
                self.sink.drone_waypoint(len(self.corners))
                self.sink.drone_mode("flying")
                await self._fly_to(*self.cfg.hold_xy, capture=False)
                await self._hold_here("survey_done")
            except AbortToHold as exc:
                self.report.aborted = str(exc)
                await self._hold_here(str(exc))
            finally:
                self.report.duration_s = self.clock() - t0
                log_event(log, "survey_report", **self.report.as_dict())

    async def hold(self) -> None:
        """Stop the survey (between bursts) and hover where the drone is."""
        self._abort = "hold_requested"
        await asyncio.to_thread(self.controls.stop)
        self.sink.drone_mode("hold")

    async def land(self) -> None:
        """Interrupt any survey, land, and wait (≤ 25 s) for z < 0.3 m."""
        self._abort = "land_requested"
        async with self._busy:
            self.sink.drone_mode("landing")
            await asyncio.to_thread(self.controls.land)
            deadline = self.clock() + self.cfg.land_timeout_s
            while self.clock() < deadline:
                p = self.pose.latest()
                if p is not None and p.z < 0.3:
                    break
                await asyncio.sleep(self.cfg.poll_s * 5)
            self.sink.drone_mode("landed")

    # --- internals ------------------------------------------------------------------
    def _fresh_pose(self) -> Pose:
        if self._abort:
            raise AbortToHold(self._abort)
        p = self.pose.latest()
        if p is None:
            raise AbortToHold("no_pose")
        age = self.clock() - p.t
        if age > self.cfg.stale_s:
            raise AbortToHold(f"pose_stale_{age:.1f}s")
        if not self.cfg.fence.contains(p.x, p.y, p.z):
            raise AbortToHold(f"outside_fence_{p.x:.1f}_{p.y:.1f}_{p.z:.1f}")
        if self._last_pose is not None:
            step = math.hypot(p.x - self._last_pose.x, p.y - self._last_pose.y)
            self.report.path_m += step
            self._since_capture += step
        self._last_pose = p
        return p

    async def _wait_alt(self, z_min: float, timeout_s: float) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            if self._abort:
                raise AbortToHold(self._abort)
            p = self.pose.latest()
            if p is not None and p.z >= z_min and self.clock() - p.t <= self.cfg.stale_s:
                return True
            await asyncio.sleep(self.cfg.poll_s * 2.5)
        return False

    async def _takeoff(self) -> None:
        p = self._fresh_pose()
        self._last_pose = None  # do not count the climb as survey path
        if p.z >= self.cfg.alt - 0.5:
            return
        self.sink.drone_mode("takeoff")
        t0 = self.clock()
        await asyncio.to_thread(self.controls.takeoff, self.cfg.alt)
        if not await self._wait_alt(min(1.0, p.z + 0.4), self.cfg.takeoff_retry_s):
            # The first command after the SDK auto-attaches its teleop policy can be lost.
            self.sink.drone_log("takeoff_retry", "Take-off command repeated", "warn")
            await asyncio.to_thread(self.controls.takeoff, self.cfg.alt)
        if not await self._wait_alt(self.cfg.alt - 0.5, self.cfg.takeoff_timeout_s):
            raise AbortToHold("takeoff_timeout")
        self.report.takeoff_s = round(self.clock() - t0, 1)
        self._last_pose = None
        self.sink.drone_log("takeoff", f"Airborne at {self.cfg.alt:.0f} m")

    async def _capture(self, p: Pose) -> None:
        t0 = self.clock()
        frame = await asyncio.to_thread(self.camera.grab)
        log_event(log, "drone_capture", s=round(self.clock() - t0, 2), ok=frame is not None)
        if frame is None:
            log_event(log, "capture_failed", level=30, pos=p.xyz)
            return
        gap = self._since_capture
        frame_id = self.sink.next_frame_id()
        path = self.frames_dir / f"{frame_id}.jpg"
        from skypaw.sim.frames import save_jpeg

        await asyncio.to_thread(save_jpeg, frame, path)
        if self.report.frames:
            self.report.max_capture_gap_m = max(self.report.max_capture_gap_m, gap)
        self.report.frames += 1
        self._since_capture = 0.0
        await self.sink.drone_frame(
            Frame(
                frame_id=frame_id,
                path=str(path),
                t=self.mission_t(),
                sensor_id=self.sensor_id,
                position=p.xyz,
                rotation=[round(v, 5) for v in p.q],
                intrinsics={**self._intrinsics, "pose_t": p.t, "capture_t": self.clock()},
            )
        )

    def _burst_speed(self, p: Pose, dist: float) -> float:
        """Commanded speed for the next 1 s burst: slows near the target, stays in the fence."""
        cfg = self.cfg
        speed = min(cfg.speed, max(cfg.min_speed, dist / cfg.burst_s))
        cos, sin = math.cos(p.yaw), math.sin(p.yaw)
        while speed > cfg.min_speed:
            d = speed * cfg.burst_s
            if cfg.fence.contains(p.x + cos * d, p.y + sin * d):
                break
            speed = max(cfg.min_speed, speed * 0.5)
        end = speed * cfg.burst_s
        if not cfg.fence.contains(p.x + cos * end, p.y + sin * end):
            raise AbortToHold("burst_would_leave_fence")
        return speed

    async def _fly_to(self, tx: float, ty: float, *, capture: bool) -> bool:
        cfg = self.cfg
        tx, ty = cfg.fence.clamp(tx, ty)
        deadline = self.clock() + cfg.leg_timeout_s
        while self.clock() < deadline:
            p = self._fresh_pose()
            if capture:
                self.sink.drone_reveal(p.x, p.y, cfg.reveal_r)
            dist = math.hypot(tx - p.x, ty - p.y)
            if dist <= cfg.tol:
                return True
            err = wrap(math.atan2(ty - p.y, tx - p.x) - p.yaw)
            tol = cfg.heading_tol_far if dist > cfg.far_m else cfg.heading_tol
            if abs(err) > tol:
                rate = cfg.turn_rate if err > 0 else -cfg.turn_rate
                dur = min(cfg.burst_s, max(cfg.min_turn_s, abs(err) / cfg.turn_rate))
                await self._cmd("turn", self.controls.turn, rate, dur, err=err, dist=dist)
                continue
            if capture and self._since_capture >= cfg.capture_min_m:
                await self._capture(p)  # frame at rest, then at most one burst of travel
            speed = self._burst_speed(p, dist)
            await self._cmd(
                "forward", self.controls.forward, speed, cfg.burst_s, err=err, dist=dist
            )
        return False

    async def _cmd(self, kind: str, fn: Callable[..., None], *args: float, **ctx: float) -> None:
        """Run a blocking flight command in a thread and log it with its wall time."""
        t0 = self.clock()
        await asyncio.to_thread(fn, *args)
        log_event(
            log,
            "drone_cmd",
            kind=kind,
            args=[round(a, 3) for a in args],
            s=round(self.clock() - t0, 2),
            **{k: round(v, 2) for k, v in ctx.items()},
        )

    async def _hold_here(self, reason: str) -> None:
        try:
            await asyncio.to_thread(self.controls.stop)
        except Exception as exc:  # noqa: BLE001 - holding must not raise
            log_event(log, "hold_stop_error", level=30, error=f"{type(exc).__name__}: {exc}")
        self.report.held = True
        self.sink.drone_mode("hold")
        level = "info" if reason == "survey_done" else "warn"
        self.sink.drone_log("hold", f"Drone holding ({reason})", level)


__all__ = [
    "AbortToHold",
    "DroneConfig",
    "DroneSink",
    "Fence",
    "SurveyDroneExecutor",
    "SurveyReport",
    "survey_corners",
]
