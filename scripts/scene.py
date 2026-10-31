#!/usr/bin/env python3
"""Sky & Paw M1: create or reuse the Cyberwave scene, then prove it works in MuJoCo.

    scripts/scene.py --project skypaw --env skypaw-m1 --duration 120 --capture
    scripts/scene.py --dry-run          # plan only: GET requests, nothing written, no sim

Steps: build rubble.glb from site.yaml -> ensure project / environment / asset (GLB upload,
MuJoCo + fixed_base PATCH) / twins (Go2, Skydio X2 at z 0.6, rubble) / 11 waypoints ->
print handles -> stop any active run -> preflight -> run_simulation(duration): MQTT over WSS
right after affect, wait for simulation_ready, capture three camera frames and >= 20 pose
samples per robot -> stop (always, also on errors and Ctrl-C).

Flags: --capture (frames + pose evidence), --no-sim (setup only), --fail-after-ready (A7
test: raise right after ready), --push-test (A6: Go2 starts 1 m from the x=-9 wall and walks
into it; the rubble's MQTT displacement is recorded; the Go2 is put back at staging after).

Exit codes:
    0    success (every requested check passed)
    1    unexpected error (simulation stopped)
    2    configuration error (no CYBERWAVE_API_KEY, bad arguments, simulation guard refused)
    3    preflight refused MuJoCo (blocking assets)
    4    simulation failed or did not become ready in time
    5    a requested check did not pass (frames, MQTT, pose samples)
    6    injected failure from --fail-after-ready (simulation stopped)
    130  interrupted by SIGINT / SIGTERM (simulation stopped)

Evidence goes to --evidence-dir (default tests/evidence/M1): console-<ts>.log (JSON lines,
incl. every REST call as "cw_http"), run-<ts>.json (timings, HTTP counts, plan, checks),
handles.json (uuids only), drone_bottom.jpg, drone_payload.jpg, go2_front.jpg,
mqtt_pose_samples.json, push_test.json. The API key is never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:  # also runnable without the editable install
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from skypaw.config import get_settings  # noqa: E402
from skypaw.logging import JsonFormatter, configure_logging, get_logger, log_event  # noqa: E402
from skypaw.scene.build_glb import build_glb_bytes, sha256  # noqa: E402
from skypaw.scene.site import load_site  # noqa: E402
from skypaw.sim.client import (  # noqa: E402
    HttpRecorder,
    MissingCredentials,
    get_client,
)
from skypaw.sim.frames import save_jpeg, wait_for_frame  # noqa: E402
from skypaw.sim.guard import SimulationOnlyViolation  # noqa: E402
from skypaw.sim.mqtt_ws import SceneMqtt  # noqa: E402
from skypaw.sim.readiness import SimulationFailed, SimulationNotReady  # noqa: E402
from skypaw.sim.scene import (  # noqa: E402
    DRONE_SLUG,
    GO2_SLUG,
    TWIN_NAMES,
    SceneContext,
    SceneHandles,
    build_scene,
    place,
)
from skypaw.sim.sim_run import (  # noqa: E402
    PreflightBlocked,
    SimRun,
    estimate_credits,
    preflight,
    run_simulation,
    stop_active,
)

log = get_logger("skypaw.scripts.scene")

(
    EXIT_OK,
    EXIT_ERROR,
    EXIT_CONFIG,
    EXIT_PREFLIGHT,
    EXIT_SIM,
    EXIT_CHECK,
    EXIT_INJECTED,
) = range(7)
EXIT_INTERRUPTED = 130

#: (twin key, sensor id, evidence file) for M1-A4.
FRAME_TARGETS = (
    ("drone", "camera_chasis_bottom", "drone_bottom.jpg"),
    ("drone", "payload_color_camera", "drone_payload.jpg"),
    ("go2", "front_camera", "go2_front.jpg"),
)
MIN_POSE_SAMPLES = 20


class InjectedFailure(RuntimeError):
    """Raised by --fail-after-ready to prove the simulation stops on errors (M1-A7)."""


def out(text: str = "") -> None:
    """User-facing CLI output on stdout (logs go to stderr / the console log)."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    s = get_settings()
    p = argparse.ArgumentParser(
        description="Create or reuse the Sky & Paw Cyberwave scene and verify it in MuJoCo.",
        epilog="Exit codes: 0 ok, 1 error, 2 config, 3 preflight, 4 sim not ready, "
        "5 check failed, 6 injected failure, 130 interrupted.",
    )
    p.add_argument("--project", default=s.cw_project_name)
    p.add_argument("--env", default=s.cw_environment_name, help="environment name")
    p.add_argument("--duration", type=float, default=float(s.sim_default_duration_s))
    p.add_argument("--capture", action="store_true", help="save frames and MQTT pose samples")
    p.add_argument("--dry-run", action="store_true", help="plan only; GET requests only")
    p.add_argument("--no-sim", action="store_true", help="set up the scene, do not simulate")
    p.add_argument("--fail-after-ready", action="store_true", help="M1-A7: raise once ready")
    p.add_argument("--push-test", action="store_true", help="M1-A6: Go2 pushes the x=-9 wall")
    p.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "tests" / "evidence" / "M1")
    p.add_argument("--ready-timeout", type=float, default=120.0)
    p.add_argument("--frame-timeout", type=float, default=40.0)
    p.add_argument("--pose-timeout", type=float, default=60.0)
    args = p.parse_args(argv)
    if not 0 < args.duration <= s.sim_max_duration_s:
        p.error(f"--duration must be in (0, {s.sim_max_duration_s}] s")
    return args


def add_file_log(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    return handler


def print_handles(env: str, h: SceneHandles) -> None:
    rows = [
        ("workspace", h.workspace_uuid),
        ("project", h.project_uuid),
        (f"environment {env}", h.environment_uuid),
        ("asset skypaw-rubble", h.rubble_asset_uuid),
        (f"twin {TWIN_NAMES['go2']} ({GO2_SLUG})", h.go2_twin_uuid),
        (f"twin {TWIN_NAMES['drone']} ({DRONE_SLUG})", h.drone_twin_uuid),
        (f"twin {TWIN_NAMES['rubble']}", h.rubble_twin_uuid),
    ] + [(f"waypoint {name}", wid) for name, wid in h.waypoints.items()]
    width = max(len(r[0]) for r in rows)
    out("Scene handles")
    for label, value in rows:
        out(f"  {label.ljust(width)}  {value or '(would be created)'}")


def print_plan(ctx: SceneContext) -> None:
    out("Plan" if ctx.dry_run else "Steps")
    for st in ctx.steps:
        out(f"  {st.action:<7} {st.kind:<11} {st.name:<16} {st.detail}")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def capture_frames(cw: Any, h: SceneHandles, ev: Path, run: SimRun, timeout_s: float) -> dict:
    """M1-A4: one usable frame per target sensor, within ``timeout_s`` of ready in total."""
    twins = {
        "drone": cw.twin(twin_id=h.drone_twin_uuid, asset_key=DRONE_SLUG),
        "go2": cw.twin(twin_id=h.go2_twin_uuid, asset_key=GO2_SLUG),
    }
    deadline = (run.t_ready or time.monotonic()) + timeout_s
    results: dict[str, Any] = {}
    for key, sensor, filename in FRAME_TARGETS:
        remaining = max(0.0, deadline - time.monotonic())
        res = wait_for_frame(twins[key], sensor, timeout_s=remaining)
        entry: dict[str, Any] = {
            "twin": key,
            "ok": res.ok,
            "attempts": res.attempts,
            "stats": res.stats.as_dict() if res.stats else None,
            "since_ready_s": round(time.monotonic() - (run.t_ready or 0), 2),
        }
        if res.frame is not None:
            entry["file"] = str(save_jpeg(res.frame, ev / filename).name)
        results[sensor] = entry
    return results


def collect_pose(mqtt: SceneMqtt, h: SceneHandles, run: SimRun, timeout_s: float) -> dict:
    """M1-A5: >= 20 position samples for the Go2 and the drone within ``timeout_s`` of ready."""
    deadline = (run.t_ready or time.monotonic()) + timeout_s
    targets = {"go2": h.go2_twin_uuid, "drone": h.drone_twin_uuid}
    while time.monotonic() < deadline:
        if all(mqtt.cache.count(u or "") >= MIN_POSE_SAMPLES for u in targets.values()):
            break
        time.sleep(0.5)
    report: dict[str, Any] = {"since_ready_s": round(time.monotonic() - (run.t_ready or 0), 2)}
    for name, u in {**targets, "rubble": h.rubble_twin_uuid}.items():
        uid = u or ""
        report[name] = {
            "twin_uuid": u,
            "position_count": mqtt.cache.count(uid, "position"),
            "rotation_count": mqtt.cache.count(uid, "rotation"),
            "latest": mqtt.cache.latest(uid),
        }
    return report


def push_test(cw: Any, mqtt: SceneMqtt, h: SceneHandles) -> dict:
    """M1-A6: drive the Go2 into the wall; report the rubble twin's MQTT displacement."""
    go2_id, rubble_id = h.go2_twin_uuid or "", h.rubble_twin_uuid or ""
    t_end = time.monotonic() + 20
    while time.monotonic() < t_end and mqtt.cache.count(go2_id) < 3:
        time.sleep(0.5)
    before = mqtt.cache.latest(rubble_id) or {}
    go2_before = mqtt.cache.latest(go2_id) or {}
    go2 = cw.twin(twin_id=go2_id, asset_key=GO2_SLUG)
    log_event(log, "push_test_move_forward", speed=0.5, duration=2)
    go2.locomotion.move_forward(0.5, duration=2)
    time.sleep(3)
    after = mqtt.cache.latest(rubble_id) or {}
    go2_after = mqtt.cache.latest(go2_id) or {}

    def dist(a: dict, b: dict) -> float | None:
        pa, pb = a.get("position"), b.get("position")
        if not pa or not pb:
            return None
        return round(sum((pa[k] - pb[k]) ** 2 for k in "xyz") ** 0.5, 4)

    return {
        "rubble_before": before.get("position"),
        "rubble_after": after.get("position"),
        "rubble_displacement_m": dist(before, after),
        "go2_before": go2_before.get("position"),
        "go2_after": go2_after.get("position"),
        "go2_travel_m": dist(go2_before, go2_after),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ev: Path = args.evidence_dir
    add_file_log(ev / f"console-{stamp}.log")
    signal.signal(signal.SIGTERM, _sigterm)
    report: dict[str, Any] = {
        "started_utc": stamp,
        "args": {k: str(v) for k, v in vars(args).items()},
        "checks": {},
    }
    budget = {"requested_duration_s": args.duration, "estimated_credits": 0.0}
    if not (args.dry_run or args.no_sim):
        budget["estimated_credits"] = estimate_credits(args.duration)
    out(
        f"Requested simulation: {0 if args.dry_run or args.no_sim else args.duration:.0f} s, "
        f"estimated {budget['estimated_credits']:.4f} credits (0.01/min)"
    )
    log_event(log, "scene_start", dry_run=args.dry_run, **budget)

    code = EXIT_OK
    recorder = HttpRecorder(read_only=args.dry_run)
    mqtt: SceneMqtt | None = None
    cw: Any = None
    handles = SceneHandles()
    ctx: SceneContext | None = None
    site = load_site()
    try:
        glb = build_glb_bytes(site)
        digest = sha256(glb)
        report["glb"] = {"bytes": len(glb), "sha256": digest, "boxes": len(site.rubble)}
        cw = get_client(settings)
        recorder.install(cw)
        ctx = SceneContext(cw, dry_run=args.dry_run)
        go2_pose = site.push_test_go2 if args.push_test else None
        handles = build_scene(
            ctx,
            site,
            project=args.project,
            environment=args.env,
            glb=glb,
            glb_sha256=digest,
            go2_pose=go2_pose,
        )
        report["setup_http"] = recorder.summary()
        report["notes"] = ctx.notes
        report["plan"] = [vars(s) for s in ctx.steps]
        log_event(log, "scene_setup_http", **recorder.summary())
        print_plan(ctx)
        print_handles(args.env, handles)
        http = report["setup_http"]
        out(
            f"Setup REST calls: {http['total']} (writes: {http['writes']}, "
            f"by method {http['by_method']})"
        )
        if not args.dry_run:
            write_json(ev / "handles.json", handles.as_dict())
        if args.dry_run or args.no_sim:
            return EXIT_OK

        env_id = handles.environment_uuid or ""
        stopped = stop_active(cw, env_id, reason="startup")
        report["stopped_at_startup"] = stopped
        report["preflight"] = {
            k: v
            for k, v in preflight(cw, env_id).items()
            if k
            in (
                "enabled",
                "eligible",
                "blocking_assets",
                "reasons",
                "available_backends",
            )
        }
        mqtt = scene_mqtt = SceneMqtt(settings)
        twin_ids = [
            u
            for u in (
                handles.go2_twin_uuid,
                handles.drone_twin_uuid,
                handles.rubble_twin_uuid,
            )
            if u
        ]

        def on_affected(run: SimRun) -> None:
            ok = scene_mqtt.connect(
                affected=run.affected, twin_uuids=twin_ids, environment_uuid=env_id
            )
            report["mqtt"] = {
                "url": scene_mqtt.url,
                "connected": ok,
                "connect_s": scene_mqtt.stats.connect_s,
                "connected_after_affect_s": round(time.monotonic() - (run.t_affect_return or 0), 2),
                "error": scene_mqtt.stats.error,
            }

        with run_simulation(
            cw,
            env_id,
            args.duration,
            ready_timeout_s=args.ready_timeout,
            on_affected=on_affected,
        ) as run:
            report["run"] = run.as_dict()
            if args.fail_after_ready:
                raise InjectedFailure("--fail-after-ready: injected failure once the sim is ready")
            if args.capture:
                report["frames"] = capture_frames(cw, handles, ev, run, args.frame_timeout)
                report["pose"] = collect_pose(scene_mqtt, handles, run, args.pose_timeout)
            if args.push_test:
                report["push_test"] = push_test(cw, scene_mqtt, handles)
                write_json(ev / "push_test.json", report["push_test"])
        report["run"] = run.as_dict()
        mqtt_ok = bool(report.get("mqtt", {}).get("connected"))
        report["checks"]["mqtt_connected"] = mqtt_ok
        if args.capture:
            frames_ok = all(f["ok"] for f in report["frames"].values())
            pose_ok = all(
                report["pose"][k]["position_count"] >= MIN_POSE_SAMPLES for k in ("go2", "drone")
            )
            report["checks"].update({"frames": frames_ok, "pose_samples": pose_ok})
        if not all(report["checks"].values()):
            code = EXIT_CHECK
    except (MissingCredentials, SimulationOnlyViolation) as exc:
        log_event(
            log,
            "scene_config_error",
            level=logging.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
        code = EXIT_CONFIG
    except PreflightBlocked as exc:
        log_event(log, "scene_preflight_blocked", level=logging.ERROR, error=str(exc))
        code = EXIT_PREFLIGHT
    except (SimulationFailed, SimulationNotReady) as exc:
        log_event(
            log,
            "scene_sim_failed",
            level=logging.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
        code = EXIT_SIM
    except InjectedFailure as exc:
        log_event(log, "scene_injected_failure", level=logging.WARNING, error=str(exc))
        code = EXIT_INJECTED
    except KeyboardInterrupt as exc:
        log_event(log, "scene_interrupted", level=logging.WARNING, signal=str(exc) or "SIGINT")
        code = EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - report every other failure with exit code 1
        log.exception("scene_error")
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = EXIT_ERROR
    finally:
        if mqtt is not None:
            mqtt.close()
            report["mqtt_stats"] = {
                "messages": mqtt.stats.messages,
                "topics": dict(mqtt.stats.topics),
                "subacks": mqtt.stats.subacks,
            }
            if args.capture:
                samples = {}
                for name, uid in (
                    ("go2", handles.go2_twin_uuid),
                    ("drone", handles.drone_twin_uuid),
                    ("rubble", handles.rubble_twin_uuid),
                ):
                    pos = mqtt.cache.samples(uid or "", "position")
                    samples[name] = {
                        "twin_uuid": uid,
                        "position_count": mqtt.cache.count(uid or "", "position"),
                        "samples": [s.as_dict() for s in pos[:200]],
                    }
                write_json(ev / "mqtt_pose_samples.json", samples)
        if args.push_test and cw is not None and ctx is not None and not args.dry_run:
            try:  # put the Go2 back at staging so the next normal run makes no writes
                place(
                    ctx,
                    handles.environment_uuid,
                    handles.go2_twin_uuid,
                    TWIN_NAMES["go2"],
                    site.spawn["go2"],
                )
            except Exception as exc:  # noqa: BLE001
                log_event(log, "push_test_restore_failed", level=logging.ERROR, error=str(exc))
        report["http"] = recorder.summary()
        report["exit_code"] = code
        log_event(log, "scene_end", exit_code=code, **budget)
        if not args.dry_run:
            write_json(ev / f"run-{stamp}.json", report)
        requested = args.duration if budget["estimated_credits"] else 0
        ready = report.get("run", {}).get("time_to_ready_s")
        out(
            f"Done (exit {code}). Requested simulation {requested:.0f} s, "
            f"estimated {budget['estimated_credits']:.4f} credits"
            + (f"; time to ready {ready} s" if ready is not None else "")
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
