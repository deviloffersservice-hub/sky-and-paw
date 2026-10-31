"""Drone survey executor state machine against a scripted kinematic drone (no Cyberwave)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from skypaw.drone.executor import DroneConfig, Fence, SurveyDroneExecutor, survey_corners
from skypaw.mission.coverage import Coverage
from skypaw.mission.fakes import KinematicDrone
from skypaw.mission.ports import Frame
from skypaw.scene.site import load_site


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class TimedDrone(KinematicDrone):
    """Each burst advances the shared clock by its duration (+0.05 s)."""

    def __init__(self, clock: Clock, *args: object, **kwargs: object) -> None:
        super().__init__(*args, clock=clock, **kwargs)  # type: ignore[arg-type]
        self._clock = clock

    def turn(self, rate: float, duration: float) -> None:
        self._clock.t += duration + 0.05
        super().turn(rate, duration)

    def forward(self, speed: float, duration: float) -> None:
        self._clock.t += duration + 0.05
        super().forward(speed, duration)

    def takeoff(self, altitude: float) -> None:
        self._clock.t += 3.0
        super().takeoff(altitude)


class Sink:
    def __init__(self) -> None:
        self.modes: list[str] = []
        self.waypoints: list[int] = []
        self.frames: list[Frame] = []
        self.logs: list[tuple[str, str]] = []
        self.coverage = Coverage()
        self.n = 0

    def drone_mode(self, mode: str) -> None:
        self.modes.append(mode)

    def drone_waypoint(self, idx: int) -> None:
        self.waypoints.append(idx)

    def drone_reveal(self, x: float, y: float, r: float) -> None:
        self.coverage.reveal(x, y, r)

    def next_frame_id(self) -> str:
        self.n += 1
        return f"f_{self.n:06d}"

    async def drone_frame(self, frame: Frame) -> None:
        self.frames.append(frame)

    def drone_log(self, key: str, text: str, level: str = "info") -> None:
        self.logs.append((key, text))


def make(tmp_path: Path, **drone_kw: object) -> tuple[SurveyDroneExecutor, TimedDrone, Sink]:
    clock = Clock()
    spawn = load_site().spawn["drone"].world
    drone = TimedDrone(clock, spawn[0], spawn[1], spawn[2], **drone_kw)
    sink = Sink()
    ex = SurveyDroneExecutor(drone, drone, drone, sink, tmp_path / "frames", clock=clock)
    return ex, drone, sink


def test_corners_are_the_site_lawnmower_in_world_frame() -> None:
    corners = survey_corners(load_site())
    assert len(corners) == 10
    assert corners[0] == (-8.0, 5.0, 6.0) and corners[1] == (8.0, 5.0, 6.0)
    assert corners[2] == (8.0, 2.5, 6.0) and corners[-1] == (8.0, -5.0, 6.0)


async def test_full_survey_reaches_corners_captures_and_holds(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path)
    await ex.start_survey()
    rep = ex.report
    assert rep.aborted is None
    assert rep.corners_reached == list(range(10))
    assert sink.modes[0] == "takeoff" and sink.modes[1] == "survey" and sink.modes[-1] == "hold"
    assert sink.waypoints == list(range(11))
    # capture spacing: never more than 2.5 m of travel between frames
    assert len(sink.frames) >= 35
    assert rep.max_capture_gap_m <= DroneConfig().capture_spacing
    for a, b in zip(sink.frames, sink.frames[1:], strict=False):
        assert math.dist(a.position[:2], b.position[:2]) <= DroneConfig().capture_spacing + 1e-6
    # frames are real files with pose + intrinsics
    f0 = sink.frames[0]
    assert Path(f0.path).is_file() and f0.sensor_id == "camera_chasis_bottom"
    assert len(f0.rotation) == 4 and f0.intrinsics["width"] == 640
    # ends at the hold point, never left the fence, coverage near complete
    end = drone.track[-1]
    assert math.hypot(end[0], end[1]) <= DroneConfig().tol
    fence = Fence()
    assert all(fence.contains(x, y, z) for x, y, z in drone.track)
    assert sink.coverage.pct_mapped() >= 95


async def test_stale_pose_aborts_to_hold(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path, freeze_after=12)
    await ex.start_survey()
    assert ex.report.aborted is not None and ex.report.aborted.startswith("pose_stale")
    assert sink.modes[-1] == "hold"
    assert drone.commands[-1][0] == "stop"
    assert len(ex.report.corners_reached) < 10


async def test_no_pose_aborts_before_any_command(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path)
    drone.latest = lambda: None  # type: ignore[method-assign]
    await ex.start_survey()
    assert ex.report.aborted == "no_pose"
    assert [c[0] for c in drone.commands] == ["stop"]


async def test_hold_interrupts_survey(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path)
    original = drone.forward
    calls = {"n": 0}

    def forward(speed: float, duration: float) -> None:
        calls["n"] += 1
        original(speed, duration)
        if calls["n"] == 5:
            ex._abort = "hold_requested"  # what hold() sets, from inside the burst thread

    drone.forward = forward  # type: ignore[method-assign]
    await ex.start_survey()
    assert ex.report.aborted == "hold_requested"
    assert calls["n"] == 5
    assert sink.modes[-1] == "hold"


async def test_burst_never_leaves_fence(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path)
    cfg = DroneConfig()
    # Facing +x right next to the east fence: the commanded burst must be shortened.
    drone.x, drone.y, drone.z, drone.yaw = 11.0, 0.0, 6.0, 0.0
    p = drone.latest()
    assert p is not None
    speed = ex._burst_speed(p, dist=10.0)
    assert 11.0 + speed * cfg.burst_s <= 12.0


async def test_land(tmp_path: Path) -> None:
    ex, drone, sink = make(tmp_path)
    await ex.land()
    assert sink.modes == ["landing", "landed"]
    assert drone.commands[-1][0] == "land"


@pytest.mark.parametrize("pt", [(0, 0, 6), (11.9, 9.9, 1), (-12, -10, 0)])
def test_fence_contains(pt: tuple[float, float, float]) -> None:
    assert Fence().contains(*pt)


@pytest.mark.parametrize("pt", [(12.1, 0, 6), (0, -10.5, 6), (0, 0, 8.5)])
def test_fence_rejects(pt: tuple[float, float, float]) -> None:
    assert not Fence().contains(*pt)
