#!/usr/bin/env python3
"""Sky & Paw v1 stream E: drive the simulated Go2 to a victim on our grid route (billable).

    scripts/go2_mission.py --env skypaw-v1-dev-e --target V-01 --duration 240
    scripts/go2_mission.py --env skypaw-v1-dev-e --target V-01 --plan-only   # no sim, no REST

Steps: plan offline (grid + Dijkstra from the Go2 spawn) -> ensure the scene in --env
(idempotent: looks everything up by name, writes only what is missing) -> stop any active
run -> preflight -> run_simulation(duration): MQTT over WSS right after affect (Go2 + rubble
twins), wait for simulation_ready, wait for the Go2 pose -> Go2Controller.navigate(victim)
-> Go2 front frame -> stop (always, in finally).

Time-to-rescue: no detector runs here, so the victim counts as detected at dispatch minus
--detected-before seconds (default 0); mission t = 0 at simulation_ready.

Evidence (--evidence-dir, default tests/evidence/V1-E): route.json (planned route + ASCII
grid), pose_trace.json (MQTT Go2 position/rotation samples during the goto and the
controller's per-burst decisions), timings.json (run, goto result, score, rubble pose
before/after), go2_front_arrival.jpg. Exit codes: 0 arrived, 1 error, 5 did not arrive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from skypaw.config import get_settings  # noqa: E402
from skypaw.go2.controller import (  # noqa: E402
    CachePoseSource,
    ControllerConfig,
    Go2Controller,
    Go2Progress,
)
from skypaw.go2.scoring import RescueScore  # noqa: E402
from skypaw.logging import configure_logging, get_logger, log_event  # noqa: E402
from skypaw.mission.grid import OccupancyGrid  # noqa: E402
from skypaw.mission.planner import plan_route  # noqa: E402
from skypaw.scene.build_glb import build_glb_bytes, sha256  # noqa: E402
from skypaw.scene.site import load_site, site_to_world  # noqa: E402
from skypaw.sim.client import get_client  # noqa: E402
from skypaw.sim.frames import frame_stats, save_jpeg, wait_for_frame  # noqa: E402
from skypaw.sim.mqtt_ws import SceneMqtt  # noqa: E402
from skypaw.sim.scene import GO2_SLUG, SceneContext, build_scene  # noqa: E402
from skypaw.sim.sim_run import (  # noqa: E402
    SimRun,
    estimate_credits,
    preflight,
    run_simulation,
    stop_active,
)

log = get_logger("skypaw.scripts.go2_mission")
EXIT_OK, EXIT_ERROR, EXIT_NOT_ARRIVED = 0, 1, 5


def write_json(path: Path, data: Any) -> None:
    """Pretty JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


class RawStateMqtt(SceneMqtt):
    """SceneMqtt that also keeps decimated raw ``free_joint_state`` / ``link_poses`` payloads.

    These topics come from the physics engine (day-one round 2, check C), so they give the
    Go2's simulated ground-truth pose to compare with the nav stack's ``position`` topic.
    """

    KINDS = ("free_joint_state", "link_poses")

    def __init__(self, *args: Any, twin_uuid: str = "", every: int = 5, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.raw: dict[str, list[dict[str, Any]]] = {k: [] for k in self.KINDS}
        self._seen = dict.fromkeys(self.KINDS, 0)
        self._uuid = twin_uuid.lower()
        self._every = every

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        super()._on_message(client, userdata, msg)
        parts = str(msg.topic).split("/")
        if len(parts) != 4 or parts[2].lower() != self._uuid or parts[3] not in self.KINDS:
            return
        kind = parts[3]
        self._seen[kind] += 1
        if self._seen[kind] % self._every:
            return
        try:
            body = json.loads(msg.payload)
        except ValueError:
            return
        self.raw[kind].append({"t": round(time.time(), 3), "body": body})


def wait_pose(mqtt: SceneMqtt, uid: str, timeout_s: float) -> dict[str, Any] | None:
    """Block until the twin has both position and rotation in the cache (or time out)."""
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        st = mqtt.cache.latest(uid) or {}
        if "position" in st and "rotation" in st:
            return st
        time.sleep(0.25)
    return None


def rubble_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Rubble twin displacement (m) and rotation change between two cache entries."""
    pb, pa = before.get("position"), after.get("position")
    out: dict[str, Any] = {
        "before": {k: before.get(k) for k in ("position", "rotation")},
        "after": {k: after.get(k) for k in ("position", "rotation")},
        "displacement_m": None,
        "rotation_changed": None,
    }
    if pb and pa:
        out["displacement_m"] = round(math.dist([pb[k] for k in "xyz"], [pa[k] for k in "xyz"]), 4)
    rb, ra = before.get("rotation"), after.get("rotation")
    if rb and ra:
        out["rotation_changed"] = max(abs(rb[k] - ra.get(k, 0.0)) for k in rb) > 1e-3
    return out


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", default=s.cw_project_name)
    p.add_argument("--env", default="skypaw-v1-dev-e")
    p.add_argument("--target", default="V-01")
    p.add_argument("--duration", type=float, default=240.0)
    p.add_argument("--goto-timeout", type=float, default=120.0)
    p.add_argument("--forward-burst", type=float, default=0.5, help="move_forward duration (s)")
    p.add_argument("--turn-burst", type=float, default=0.3, help="turn_* duration (s)")
    p.add_argument("--detected-before", type=float, default=0.0)
    p.add_argument("--plan-only", action="store_true", help="plan and write route.json only")
    p.add_argument(
        "--record-raw",
        action="store_true",
        help="also save Go2 free_joint_state / link_poses (sim ground truth) to raw_state.json",
    )
    p.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "tests" / "evidence" / "V1-E")
    args = p.parse_args(argv)
    if not 0 < args.duration <= min(300, s.sim_max_duration_s):
        p.error("--duration must be in (0, 300] s (PROTOCOL budget)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    ev: Path = args.evidence_dir
    ev.mkdir(parents=True, exist_ok=True)
    site = load_site()
    victim = next((v for v in site.victims if v["id"] == args.target), None)
    if victim is None:
        log_event(log, "go2_mission_bad_target", level=40, target=args.target)
        return EXIT_ERROR
    goal = list(site_to_world(*victim["pos"]))
    spawn = site.spawn["go2"].world
    grid = OccupancyGrid.from_site(site)
    planned = plan_route(grid, spawn, goal)
    marks = {c: "*" for c in planned.cells}
    marks[grid.spec.cell_of(goal[0], goal[1])] = "V"
    marks[grid.spec.cell_of(spawn[0], spawn[1])] = "G"
    write_json(
        ev / "route.json",
        {
            "target": args.target,
            "goal_world": goal,
            "spawn_world": list(spawn),
            "planned_from_spawn": planned.as_dict(),
            "grid_ascii_north_up": grid.render(marks).split("\n"),
        },
    )
    log_event(
        log,
        "go2_mission_plan",
        target=args.target,
        length=round(planned.length, 2),
        est_s=round(planned.seconds, 1),
        points=planned.points,
    )
    if args.plan_only:
        return EXIT_OK

    log_event(
        log,
        "go2_mission_budget",
        requested_duration_s=args.duration,
        estimated_credits=estimate_credits(args.duration),
    )
    report: dict[str, Any] = {"target": args.target, "goal_world": goal, "env": args.env}
    code = EXIT_ERROR
    mqtt: SceneMqtt = SceneMqtt(settings)
    try:
        cw = get_client(settings)
        ctx = SceneContext(cw)
        glb = build_glb_bytes(site)
        h = build_scene(
            ctx, site, project=args.project, environment=args.env, glb=glb, glb_sha256=sha256(glb)
        )
        report["setup_steps"] = [f"{s.action} {s.kind} {s.name}" for s in ctx.steps]
        env_id, go2_id, rubble_id = h.environment_uuid, h.go2_twin_uuid, h.rubble_twin_uuid
        if not (env_id and go2_id and rubble_id):
            raise RuntimeError(f"scene incomplete: {h.as_dict()}")
        report["handles"] = {"environment": env_id, "go2": go2_id, "rubble": rubble_id}
        if args.record_raw:
            mqtt = RawStateMqtt(settings, twin_uuid=go2_id)
        report["stopped_at_startup"] = stop_active(cw, env_id, reason="startup")
        preflight(cw, env_id)

        def on_affected(run: SimRun) -> None:
            report["mqtt_connected"] = mqtt.connect(affected=True, twin_uuids=[go2_id, rubble_id])

        with run_simulation(cw, env_id, args.duration, on_affected=on_affected) as run:
            t_ready = time.monotonic()
            report["time_to_ready_s"] = run.time_to_ready_s
            st = wait_pose(mqtt, go2_id, 40)
            report["go2_pose_wait_s"] = round(time.monotonic() - t_ready, 1)
            if st is None:
                raise RuntimeError("no Go2 pose on MQTT within 40 s of simulation_ready")
            report["go2_start_pose"] = {k: st.get(k) for k in ("position", "rotation")}
            rubble_before = mqtt.cache.latest(rubble_id) or {}
            go2 = cw.twin(twin_id=go2_id, asset_key=GO2_SLUG)

            score = RescueScore(f"go2-{args.env}")
            n = {"k": 0}

            def on_progress(p: Go2Progress) -> None:
                n["k"] += 1
                if n["k"] % 10 == 1 or p.mode != "moving":
                    log_event(
                        log,
                        "go2_progress",
                        t=round(p.t, 1),
                        mode=p.mode,
                        action=p.action,
                        pos=[round(v, 2) for v in p.pos] if p.pos else None,
                        wp=p.waypoint_idx,
                        remaining_m=round(p.dist_remaining, 2) if p.dist_remaining else None,
                    )

            cfg = ControllerConfig(
                timeout_s=args.goto_timeout,
                forward_burst_s=args.forward_burst,
                turn_burst_s=args.turn_burst,
            )
            ctrl = Go2Controller(
                go2,
                CachePoseSource(mqtt.cache, go2_id),
                grid=grid,
                config=cfg,
                on_progress=on_progress,
            )
            t_dispatch = time.monotonic() - t_ready
            score.detected(args.target, t_dispatch - args.detected_before)
            score.dispatched(args.target, t_dispatch)
            n_pos0 = mqtt.cache.count(go2_id, "position")
            wall0 = time.time()
            result = asyncio.run(ctrl.navigate(goal, args.target))
            t_end = time.monotonic() - t_ready
            if result.arrived:
                score.arrived(args.target, t_end, result.distance_error_m)
                code = EXIT_OK
            else:
                score.failed(args.target)
                code = EXIT_NOT_ARRIVED
            time.sleep(1.0)  # let the rubble settle before the "after" pose
            rubble_after = mqtt.cache.latest(rubble_id) or {}
            report["rubble"] = rubble_delta(rubble_before, rubble_after)
            report["goto"] = result.as_dict()
            report["score"] = score.as_dict()
            report["go2_position_msgs_during_goto"] = mqtt.cache.count(go2_id, "position") - n_pos0
            write_json(
                ev / "pose_trace.json",
                {
                    "mqtt_position": [
                        s.as_dict() for s in mqtt.cache.samples(go2_id, "position") if s.t >= wall0
                    ],
                    "mqtt_rotation": [
                        s.as_dict() for s in mqtt.cache.samples(go2_id, "rotation") if s.t >= wall0
                    ],
                    "controller": result.trace,
                },
            )
            if isinstance(mqtt, RawStateMqtt):
                write_json(ev / "raw_state.json", {"go2": go2_id, **mqtt.raw})
            frame = wait_for_frame(go2, "front_camera", timeout_s=15)
            if frame.frame is not None:
                save_jpeg(frame.frame, ev / "go2_front_arrival.jpg")
                report["arrival_frame"] = frame_stats(frame.frame).as_dict()
            log_event(
                log,
                "go2_mission_result",
                status=result.status,
                seconds=round(result.seconds, 1),
                distance_error_m=report["goto"]["distance_error_m"],
                rubble_displacement_m=report["rubble"]["displacement_m"],
                score=score.totals(),
            )
        report["run"] = run.as_dict()
    except Exception as exc:  # noqa: BLE001
        log.exception("go2_mission_error")
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = EXIT_ERROR
    finally:
        mqtt.close()
        report["mqtt"] = {
            "messages": mqtt.stats.messages,
            "error": mqtt.stats.error,
            "topics": dict(mqtt.stats.topics),
        }
        write_json(ev / "timings.json", report)
        log_event(log, "go2_mission_end", exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
