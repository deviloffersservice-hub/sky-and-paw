"""Offline fake of the Go2 for tests and for other streams (PROTOCOL: each stream ships a fake).

:class:`KinematicGo2` is a unicycle robot on a virtual clock that answers the same
``twin.locomotion.*`` calls the SDK twin does, and is its own pose source. Wiring it into
the real :class:`~skypaw.go2.controller.Go2Controller` (:func:`fake_go2_executor`) gives a
``ports.Go2Executor`` that plans, "drives" and reports progress in milliseconds of wall time.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from skypaw.go2.controller import (
    ControllerConfig,
    Go2Controller,
    PoseReading,
    ProgressCallback,
)
from skypaw.mission.grid import OccupancyGrid


@dataclass
class VirtualClock:
    """A manually advanced clock (seconds)."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        """Move time forward by ``dt`` seconds."""
        self.now += float(dt)

    async def sleep(self, dt: float) -> None:
        """``asyncio.sleep`` replacement: advances the clock and yields once."""
        self.advance(dt)
        await asyncio.sleep(0)


@dataclass
class _Locomotion:
    robot: KinematicGo2

    def move_forward(self, distance: float = 0.3, *, duration: float = 1.0, **_: Any) -> None:
        self.robot.drive(abs(distance), 0.0, duration, "forward")

    def turn_left(self, angle: float = 0.5, *, duration: float = 1.0, **_: Any) -> None:
        self.robot.drive(0.0, abs(angle), duration, "turn_left")

    def turn_right(self, angle: float = 0.5, *, duration: float = 1.0, **_: Any) -> None:
        self.robot.drive(0.0, -abs(angle), duration, "turn_right")

    def move(
        self,
        *,
        linear_x: float | None = None,
        angular_z: float | None = None,
        duration: float = 0.0,
        **_: Any,
    ) -> None:
        """``locomotion.move``: ``move_sign`` models the sim's angular_z convention."""
        v, w = float(linear_x or 0.0), float(angular_z or 0.0)
        name = "forward" if not w else ("turn_left" if w > 0 else "turn_right")
        if self.robot.move_sign == 0.0:
            self.robot.calls.append(name)
            self.robot.clock.advance(duration)
            self.robot._publish()
            return
        self.robot.drive(v, w * self.robot.move_sign, duration, name)

    def stop(self, **_: Any) -> None:
        self.robot.calls.append("stop")


@dataclass
class KinematicGo2:
    """Unicycle Go2: ``speed_scale`` < 1 models a slower real response; ``blocked`` freezes it."""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    z: float = 0.35
    clock: VirtualClock = field(default_factory=VirtualClock)
    speed_scale: float = 1.0
    blocked: bool = False
    publish_pose: bool = True
    move_sign: float = 1.0  # +1 angular_z>0 is CCW, -1 inverted, 0 move() has no effect
    calls: list[str] = field(default_factory=list)
    _last_pose: PoseReading | None = None
    on_move: Callable[[KinematicGo2], None] | None = None

    def __post_init__(self) -> None:
        self.locomotion = _Locomotion(self)
        self._publish()

    def _publish(self) -> None:
        if self.publish_pose:
            self._last_pose = PoseReading(self.x, self.y, self.z, self.yaw, self.clock())

    def drive(self, v: float, w: float, duration: float, name: str) -> None:
        """Apply a velocity burst for ``duration`` seconds (exact unicycle integration)."""
        self.calls.append(name)
        if not self.blocked:
            s = self.speed_scale
            if w:
                self.yaw = (self.yaw + w * s * duration + math.pi) % (2 * math.pi) - math.pi
            else:
                self.x += math.cos(self.yaw) * v * s * duration
                self.y += math.sin(self.yaw) * v * s * duration
        self.clock.advance(duration)
        self._publish()
        if self.on_move is not None:
            self.on_move(self)

    def latest(self) -> PoseReading | None:
        """The last published pose (``PoseSource``)."""
        return self._last_pose


async def run_inline(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``run_blocking`` for fakes: call in the loop thread, then yield once."""
    out = fn(*args, **kwargs)
    await asyncio.sleep(0)
    return out


def fake_go2_executor(
    start: tuple[float, float, float] = (-11.0, -7.5, 0.0),
    *,
    grid: OccupancyGrid | None = None,
    config: ControllerConfig | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[Go2Controller, KinematicGo2]:
    """A real controller driving a :class:`KinematicGo2` at ``start`` = (x, y, yaw)."""
    robot = KinematicGo2(x=start[0], y=start[1], yaw=start[2])
    ctrl = Go2Controller(
        robot,
        robot,
        grid=grid,
        config=config,
        on_progress=on_progress,
        clock=robot.clock,
        sleep=robot.clock.sleep,
        run_blocking=run_inline,
    )
    return ctrl, robot


__all__ = ["KinematicGo2", "VirtualClock", "fake_go2_executor", "run_inline"]
