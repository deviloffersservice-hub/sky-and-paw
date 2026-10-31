"""Stream E: Go2Controller state machine against a scripted kinematic Go2, and scoring."""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from skypaw.go2.controller import (
    CachePoseSource,
    ControllerConfig,
    Go2Controller,
    Go2Progress,
    PoseReading,
    goto_timeout,
    wrap,
    yaw_of,
)
from skypaw.go2.fake import KinematicGo2, fake_go2_executor, run_inline
from skypaw.go2.scoring import RescueScore
from skypaw.mission.grid import BLOCKED, OccupancyGrid
from skypaw.mission.planner import plan_route
from skypaw.mission.ports import Go2Executor
from skypaw.scene.site import load_site, site_to_world
from skypaw.sim.mqtt_ws import PoseCache, PoseSample

SITE = load_site()
V01 = list(site_to_world(*next(v for v in SITE.victims if v["id"] == "V-01")["pos"]))
GRID = OccupancyGrid.from_site(SITE)


def _ctrl(robot: KinematicGo2, **cfg: float) -> Go2Controller:
    return Go2Controller(
        robot,
        robot,
        grid=GRID,
        config=ControllerConfig(**cfg),
        clock=robot.clock,
        sleep=robot.clock.sleep,
        run_blocking=run_inline,
    )


def test_satisfies_port() -> None:
    ctrl, _ = fake_go2_executor(grid=GRID)
    port: Go2Executor = ctrl  # structural: goto(position, target_id) / stop()
    assert asyncio.iscoroutinefunction(port.goto) and asyncio.iscoroutinefunction(port.stop)


async def test_turns_in_place_then_moves_then_arrives() -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=math.pi / 2)  # facing north, goal is east
    ctrl = _ctrl(robot)
    res = await ctrl.navigate([-8.0, -7.5, 0.0], "T-1")
    assert res.status == "arrived" and res.arrived
    assert res.distance_error_m is not None and res.distance_error_m <= 0.7
    moves = [c for c in robot.calls if c != "stop"]
    first_forward = moves.index("forward")
    assert first_forward > 0 and set(moves[:first_forward]) == {"turn_right"}
    # 0.6 rad/s x 0.3 s = 0.18 rad per burst < 0.2 tolerance: no left/right dithering
    assert "turn_left" not in moves[:first_forward]
    assert robot.calls[-1] == "stop"
    assert ctrl.mode == "arrived" and ctrl.last_result is res
    turn_s = sum(r["dur"] for r in res.turn_rates)
    assert res.seconds == pytest.approx(turn_s + 0.5 * res.forwards, abs=1e-6)
    assert [r["cmd"] for r in res.turn_rates][:2] == [0.8, 0.8]  # 90 deg error: big turns first


async def test_full_route_to_v01_from_spawn_never_enters_blocked() -> None:
    progress: list[Go2Progress] = []
    ctrl, robot = fake_go2_executor((-11.0, -7.5, 0.0), grid=GRID, on_progress=progress.append)
    visited: list[tuple[float, float]] = []
    robot.on_move = lambda r: visited.append((r.x, r.y))
    await ctrl.goto(V01, "V-01")
    res = ctrl.last_result
    assert res is not None and res.status == "arrived"
    assert res.distance_error_m is not None and res.distance_error_m <= 0.7
    assert res.seconds < 120
    assert all(GRID.class_at(x, y) != BLOCKED for x, y in visited)
    assert progress[0].mode == "moving" and progress[-1].mode == "arrived"
    assert all(p.target == "V-01" for p in progress)
    mid = progress[len(progress) // 2]
    assert mid.path[0][:2] == pytest.approx(mid.pos[:2])
    assert mid.path[-1][:2] == pytest.approx(V01[:2])
    assert set(mid.snapshot_fields()) == {"pos", "yaw", "mode", "target", "path", "pose_age_s"}
    remaining = [p.dist_remaining for p in progress if p.dist_remaining is not None]
    assert remaining[-1] < remaining[0]
    json.dumps(res.as_dict(with_trace=True))  # evidence-serialisable


async def test_aborts_on_stale_pose() -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=0.0)
    count = {"n": 0}

    def freeze(r: KinematicGo2) -> None:
        count["n"] += 1
        if count["n"] == 3:
            r.publish_pose = False  # MQTT goes quiet; the clock keeps running

    robot.on_move = freeze
    res = await _ctrl(robot).navigate([-5.0, -7.5, 0.0], "T-2")
    assert res.status == "stale_pose"
    assert robot.calls[-1] == "stop"
    # aborts within one burst after the 3 s age limit
    assert 3.0 < robot.clock() - robot.latest().t <= 3.5


async def test_no_pose_at_all_aborts_quickly() -> None:
    robot = KinematicGo2(publish_pose=False)
    robot._last_pose = None
    ctrl = _ctrl(robot)
    res = await ctrl.navigate([0.0, -7.5, 0.0], None)
    assert res.status == "stale_pose" and res.route is None
    assert res.seconds <= 3.2
    assert ctrl.mode == "blocked"


_P1 = plan_route(GRID, (-11.0, -7.5), (-5.0, -7.5)).points[1]


@pytest.mark.parametrize(
    ("yaw", "kind"), [(math.atan2(_P1[1] + 7.5, _P1[0] + 11.0), "forward"), (2.0, "turn")]
)
async def test_stuck_against_rubble_is_blocked(yaw: float, kind: str) -> None:
    # facing the first route point -> forward bursts, else turns; none move
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=yaw, blocked=True)
    ctrl = _ctrl(robot)
    res = await ctrl.navigate([-5.0, -7.5, 0.0], "T-3")
    assert res.status == "stuck" and ctrl.mode == "blocked"
    assert res.turns + res.forwards == 12
    assert (res.forwards if kind == "forward" else res.turns) == 12


async def test_timeout() -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=0.0, speed_scale=0.5)
    res = await _ctrl(robot, timeout_s=5.0).navigate(V01, "V-01")
    assert res.status == "timeout"
    assert 5.0 < res.seconds <= 5.6


async def test_stop_interrupts_and_sends_stop() -> None:
    ctrl, robot = fake_go2_executor((-11.0, -7.5, 0.0), grid=GRID)
    moving = asyncio.Event()
    robot.on_move = lambda r: moving.set() if r.calls.count("forward") >= 3 else None
    task = asyncio.create_task(ctrl.navigate(V01, "V-01"))
    await moving.wait()
    await ctrl.stop()
    res = await task
    assert res.status == "stopped" and ctrl.mode == "idle"
    assert "stop" in robot.calls


async def test_no_route() -> None:
    import numpy as np

    seen = np.zeros((36, 52), dtype=bool)
    seen[0:2, 0:2] = True
    seen[30:32, 40:42] = True
    grid = OccupancyGrid.from_site(SITE, seen=seen, respect_seen=True)
    robot = KinematicGo2(x=-12.75, y=-8.75)
    ctrl = Go2Controller(
        robot, robot, grid=grid, clock=robot.clock, sleep=robot.clock.sleep, run_blocking=run_inline
    )
    res = await ctrl.navigate([7.25, 6.25, 0.0], "T-4")
    assert res.status == "no_route" and ctrl.mode == "blocked"


@pytest.mark.parametrize("sdk_mode", ["live", "real-world", None])
async def test_refuses_to_move_unless_simulation(sdk_mode: str | None) -> None:
    from types import SimpleNamespace

    from skypaw.sim.guard import SimulationOnlyViolation

    robot = KinematicGo2(x=-11.0, y=-7.5)
    robot.client = SimpleNamespace(config=SimpleNamespace(runtime_mode=sdk_mode))
    with pytest.raises(SimulationOnlyViolation):
        await _ctrl(robot).navigate([-8.0, -7.5, 0.0], "T-5")
    assert robot.calls == []


async def test_refuses_when_configured_mode_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    from skypaw.sim.guard import SimulationOnlyViolation

    monkeypatch.setenv("CW_RUNTIME_MODE", "live")
    robot = KinematicGo2(x=-11.0, y=-7.5)
    with pytest.raises(SimulationOnlyViolation):
        await _ctrl(robot).navigate([-8.0, -7.5, 0.0], "T-6")
    assert robot.calls == []


def test_cache_pose_source_reads_mqtt_cache() -> None:
    cache = PoseCache()
    uid = "7539f1bf-ad22-4dfb-a2b0-a2639e05660a"
    src = CachePoseSource(cache, uid)
    assert src.latest() is None
    cache.update(PoseSample(uid, "position", {"x": 1.0, "y": 2.0, "z": 0.35}, t=100.0))
    assert src.latest() is None  # no rotation yet
    q = {"x": 0.0, "y": 0.0, "z": math.sin(0.25), "w": math.cos(0.25)}
    cache.update(PoseSample(uid, "rotation", q, t=100.2))
    p = src.latest()
    assert p == PoseReading(1.0, 2.0, 0.35, pytest.approx(0.5), 100.0)


def test_angle_helpers() -> None:
    assert yaw_of({"x": 0, "y": 0, "z": 1, "w": 0}) == pytest.approx(math.pi)
    assert wrap(3 * math.pi / 2) == pytest.approx(-math.pi / 2)


# --- scoring ----------------------------------------------------------------------------


def test_scoring_math() -> None:
    s = RescueScore("SP-0417")
    s.detected("V-01", 100.0)
    s.detected("V-01", 140.0)  # later detection does not move it
    s.dispatched("V-01", 112.5)
    s.arrived("V-01", 160.0, distance_error_m=0.41)
    s.detected("V-02", 120.0)
    s.dispatched("V-02", 170.0)
    s.arrived("V-02", 230.0)
    s.detected("V-03", 150.0)
    s.dispatched("V-03", 240.0)
    s.failed("V-03")
    v1 = s.victims["V-01"]
    assert (v1.wait_s, v1.travel_s, v1.time_to_rescue_s) == (12.5, 47.5, 60.0)
    t = s.totals()
    assert t == {
        "victims": 3,
        "detected": 3,
        "reached": 2,
        "unreached": ["V-03"],
        "time_to_rescue_total_s": 170.0,
        "time_to_rescue_mean_s": 85.0,
        "time_to_rescue_max_s": 110.0,
    }
    data = json.loads(s.to_json())
    assert [v["victim_id"] for v in data["victims"]] == ["V-01", "V-02", "V-03"]
    assert data["victims"][2]["status"] == "failed"
    assert data["victims"][0]["distance_error_m"] == 0.41


def test_scoring_empty() -> None:
    t = RescueScore().totals()
    assert t["reached"] == 0 and t["time_to_rescue_mean_s"] is None


# --- v1 tune: route timeout, big turns, signed move() turning, achieved yaw rate --------------


def test_goto_timeout_from_route() -> None:
    cfg = ControllerConfig()
    assert goto_timeout(cfg, 10.0) == 120.0
    assert goto_timeout(cfg, 40.0) == pytest.approx(130.0)
    assert goto_timeout(cfg, 200.0) == 300.0
    assert goto_timeout(ControllerConfig(timeout_s=5.0), 200.0) == 5.0


async def test_route_timeout_recorded_on_result() -> None:
    ctrl, _ = fake_go2_executor((-11.0, -7.5, 0.0), grid=GRID)
    res = await ctrl.navigate(V01, "V-01")
    assert res.timeout_s == pytest.approx(max(120.0, 2.5 * res.route.seconds + 30.0))
    assert res.as_dict()["timeout_s"] == res.timeout_s


async def test_big_turns_then_small_turns_and_rates_logged() -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=math.pi)  # facing away from the goal
    res = await _ctrl(robot).navigate([-8.0, -7.5, 0.0], "T-7")
    assert res.status == "arrived"
    cmds = [r["cmd"] for r in res.turn_rates]
    assert cmds[0] == 0.8 and 0.6 in cmds and cmds.index(0.6) > cmds.index(0.8)
    for r in res.turn_rates:  # the kinematic robot achieves exactly what is commanded
        assert r["achieved"] == pytest.approx(r["cmd"], abs=1e-3)
    assert any("yaw_rate" in row for row in res.trace)
    summary = res.as_dict()["turn_rate"]
    assert set(summary) == {"0.6", "0.8"}


@pytest.mark.parametrize(
    ("move_sign", "verdict", "api", "sign"),
    [
        (1.0, "sign_ok", "move", 1.0),
        (-1.0, "sign_flipped", "move", -1.0),
        (0.0, "no_effect_fallback_named", "named", 1.0),
    ],
)
async def test_turn_self_check(move_sign: float, verdict: str, api: str, sign: float) -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=math.pi / 2, move_sign=move_sign)
    ctrl = _ctrl(robot, turn_api="move")
    res = await ctrl.navigate([-8.0, -7.5, 0.0], "T-8")
    assert res.status == "arrived"
    cal = res.turn_calibration
    assert cal is not None and cal["verdict"] == verdict
    assert (cal["now_api"], cal["now_sign"]) == (api, sign)
    assert cal["requested"] == "turn_right"


async def test_named_turns_are_the_default() -> None:
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=math.pi / 2, move_sign=0.0)  # move() inert
    ctrl = _ctrl(robot)
    assert ctrl.config.turn_api == "named"
    res = await ctrl.navigate([-8.0, -7.5, 0.0], "T-9")
    assert res.status == "arrived" and res.turn_calibration["verdict"] == "sign_ok"


async def test_move_refused_by_sdk_catalog_falls_back_to_named() -> None:
    # live run 4: cyberwave 0.7.4 raises ValueError("Command 'move' is not in the MQTT catalog")
    robot = KinematicGo2(x=-11.0, y=-7.5, yaw=math.pi / 2, move_in_catalog=False)
    ctrl = _ctrl(robot, turn_api="move")
    res = await ctrl.navigate([-8.0, -7.5, 0.0], "T-10")
    assert res.status == "arrived"
    assert ctrl._turn_api == "named" and "turn_right" in robot.calls
