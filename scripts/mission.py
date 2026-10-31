#!/usr/bin/env python3
"""Sky & Paw v1: run the mission runtime headless (fakes for Go2, detector and voice).

    scripts/mission.py --env skypaw-v1 --duration 300 --autostart-survey [--serve]
    scripts/mission.py --offline --autostart-survey      # kinematic drone, no Cyberwave

Live mode: create/reuse the scene (look up by name), start MuJoCo through
``run_simulation`` (``duration`` <= 300 s, always stopped in ``finally``), MQTT pose over
WSS, and the real drone executor. ``--autostart-survey`` proposes and auto-confirms the
aerial survey as soon as the simulation is ready; the run ends when the survey is over
(``--linger`` s later), when the simulation ends, or on Ctrl-C. ``--serve`` also runs the
FastAPI app (``/ws``, ``/api/state``) on ``--port`` in the same process and checks
``/api/state`` while the survey runs. Snapshot summaries are logged every ``--every`` s.

Evidence (``--evidence-dir``): ``snapshot.json`` (mid-survey ``/api/state`` + final),
``timings.json`` (run, survey report, frame count, pct_mapped, api check) and three frames.
Exit codes: 0 ok, 1 error, 4 simulation error, 5 survey incomplete (< 6 corners).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from skypaw.config import get_settings  # noqa: E402
from skypaw.logging import configure_logging, get_logger, log_event  # noqa: E402
from skypaw.mission.runtime import MissionRuntime  # noqa: E402
from skypaw.mission.sim_session import SimSession  # noqa: E402
from skypaw.mission.state import SNAPSHOT_KEYS  # noqa: E402

log = get_logger("skypaw.scripts.mission")
MAX_DURATION_S = 300.0


def summary(snap: dict[str, Any]) -> dict[str, Any]:
    """Compact view of a snapshot for the console."""
    d = snap["robots"]["drone"]
    return {
        "t": snap["t"],
        "phase": snap["mission"]["phase"],
        "sim": snap["sim"]["status"],
        "sim_elapsed_s": snap["sim"]["elapsed_s"],
        "drone_mode": d["mode"],
        "drone_pos": d["pos"],
        "pose_age_s": d["pose_age_s"],
        "waypoint_idx": snap["survey"]["waypoint_idx"],
        "frames": snap["survey"]["frames"],
        "pct_mapped": snap["survey"]["pct_mapped"],
        "victims": [v["id"] for v in snap["victims"]],
        "decisions": len(snap["decisions"]),
    }


def snapshot_valid(snap: dict[str, Any]) -> list[str]:
    """Missing PROTOCOL keys (empty list = valid)."""
    missing: list[str] = []
    for path, keys in SNAPSHOT_KEYS.items():
        obj: Any = snap
        for part in filter(None, path.split(".")):
            obj = obj.get(part, {}) if isinstance(obj, dict) else {}
        missing += [f"{path}.{k}".lstrip(".") for k in keys if k not in obj]
    return missing


def pick_port(preferred: int) -> int:
    """``preferred`` if free on 127.0.0.1, else an ephemeral free port (logged)."""
    import socket

    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            chosen = int(s.getsockname()[1])
        if chosen != preferred:
            log_event(log, "port_in_use", level=30, wanted=preferred, using=chosen)
        return chosen
    raise RuntimeError("no free port")


async def check_api(port: int) -> dict[str, Any]:
    """GET /api/state over HTTP from the running server."""
    import httpx

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        r = await client.get(f"http://127.0.0.1:{port}/api/state")
    body = r.json()
    return {
        "status": r.status_code,
        "ms": round((time.monotonic() - t0) * 1000, 1),
        "missing_keys": snapshot_valid(body),
        "summary": summary(body),
        "snapshot": body,
    }


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.data_dir:
        settings = settings.model_copy(update={"data_dir": Path(args.data_dir)})
    # Headless default: fakes for the Go2, voice and detector (--real-* to opt in).
    settings = settings.model_copy(
        update={
            "skypaw_fake_go2": not args.real_go2,
            "skypaw_fake_voice": not args.real_voice,
            "skypaw_fake_detector": not args.real_detector,
        }
    )
    if args.stop_file is not None:
        args.stop_file.unlink(missing_ok=True)
    ev: Path = args.evidence_dir
    ev.mkdir(parents=True, exist_ok=True)
    sim: SimSession | None = None
    rt: MissionRuntime
    if args.offline:
        from skypaw.drone.executor import SurveyDroneExecutor
        from skypaw.mission.fakes import KinematicDrone

        rt = MissionRuntime(settings, autostart_survey=args.autostart_survey)
        kd = KinematicDrone(-11.5, -5.5, 0.6)
        rt.drone = SurveyDroneExecutor(kd, kd, kd, rt, rt.frames_dir, mission_t=rt.state.t)
    else:
        sim = SimSession(args.env, args.duration, settings=settings)
        rt = MissionRuntime(settings, sim=sim, autostart_survey=args.autostart_survey)

    server_task: asyncio.Task[Any] | None = None
    server: Any = None
    if args.serve:
        import uvicorn

        args.port = pick_port(args.port)
        from skypaw.app import create_app

        app = create_app(settings, runtime=rt)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        )
        server_task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    me = asyncio.current_task()
    if me is not None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, me.cancel)
    report: dict[str, Any] = {
        "env": args.env,
        "offline": args.offline,
        "duration_s": args.duration,
    }
    snapshots: dict[str, Any] = {}
    code = 0
    t0 = time.monotonic()
    try:
        await rt.start()
        last_print = 0.0
        survey_seen = False
        api_checked = False
        done_at: float | None = None
        while True:
            await asyncio.sleep(0.5)
            snap = rt.snapshot()
            now = time.monotonic()
            if now - last_print >= args.every:
                last_print = now
                log_event(log, "snapshot", **summary(snap))
            if rt.state.sim.ready and report.get("ready_after_s") is None:
                report["ready_after_s"] = round(now - t0, 1)
            if rt.surveys_started:
                survey_seen = True
                if args.serve and not api_checked and snap["survey"]["frames"] >= 5:
                    try:
                        res = await check_api(args.port)
                        snapshots["api_state_mid_survey"] = res.pop("snapshot")
                        report["api_check"] = res
                        api_checked = True
                        log_event(
                            log,
                            "api_state_check",
                            **{k: res[k] for k in ("status", "ms")},
                        )
                    except Exception as exc:  # noqa: BLE001
                        report["api_check"] = {"error": f"{type(exc).__name__}: {exc}"}
                        api_checked = True
            if survey_seen and rt.surveys_finished and done_at is None:
                done_at = now
                log_event(log, "survey_finished", **summary(snap))
            if args.stop_file is not None:
                if args.stop_file.exists():  # an external driver (drive_mission.py) is done
                    log_event(log, "stop_file_seen", path=str(args.stop_file))
                    break
            elif done_at is not None and now - done_at >= args.linger:
                break
            if rt.sim_done.is_set() or (not args.offline and rt.state.sim.status == "error"):
                break
            if now - t0 > args.duration + 180:
                log_event(log, "mission_wall_timeout", level=30)
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        log_event(log, "mission_interrupted", level=30)
        code = 130
    except Exception:
        log.exception("mission_error")
        code = 1
    finally:
        final = rt.snapshot()
        snapshots["final"] = final
        await rt.stop()  # holds the drone, stops the simulation (run_simulation finally)
        if server is not None:
            server.should_exit = True
            if server_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server_task, 10)
        drone_report = getattr(rt.drone, "report", None)
        report.update(
            {
                "wall_s": round(time.monotonic() - t0, 1),
                "survey": drone_report.as_dict() if drone_report is not None else None,
                "frames": final["survey"]["frames"],
                "pct_mapped": final["survey"]["pct_mapped"],
                "victims": [v["id"] for v in final["victims"]],
                "sim": final["sim"],
                "sim_session": None
                if sim is None
                else {
                    "run": sim.info.run,
                    "mqtt_connected": sim.info.mqtt_connected,
                    "error": sim.info.error,
                    "env_id": sim.info.env_id,
                },
                "final_missing_keys": snapshot_valid(final),
            }
        )
        frames = sorted(rt.frames_dir.glob("f_*.jpg"))
        if frames:
            picks = [frames[0], frames[len(frames) // 2], frames[-1]]
            for f in dict.fromkeys(picks):
                shutil.copy2(f, ev / f.name)
            report["evidence_frames"] = [f.name for f in dict.fromkeys(picks)]
        det_report = getattr(rt.detector, "report", None)
        if callable(det_report):  # stream B pipeline: per-frame records + candidates
            det = det_report()
            (ev / "detections.json").write_text(
                json.dumps(det, indent=1, default=str) + "\n", "utf-8"
            )
            hit_ids = [r["frame_id"] for r in det.get("frames", []) if r.get("hits")]
            for fid in hit_ids[:8]:
                src = rt.frames_dir / f"{fid}.jpg"
                if src.is_file():
                    shutil.copy2(src, ev / f"det_{fid}.jpg")
            report["detection_frames"] = hit_ids
            report["detection_latency_ms"] = det.get("latency_ms")
        report["published"] = rt.published
        (ev / "snapshot.json").write_text(json.dumps(snapshots, indent=1) + "\n", "utf-8")
        (ev / "timings.json").write_text(json.dumps(report, indent=2, default=str) + "\n", "utf-8")
        log_event(
            log,
            "mission_end",
            exit_code=code,
            **{k: report[k] for k in ("frames", "pct_mapped")},
        )
    if code:
        return code
    if final["sim"]["status"] == "error":
        return 4
    survey = report.get("survey") or {}
    if args.autostart_survey and len(survey.get("corners_reached", [])) < 6:
        return 5
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Headless Sky & Paw mission runtime.")
    ap.add_argument("--env", default="skypaw-v1")
    ap.add_argument("--duration", type=float, default=MAX_DURATION_S)
    ap.add_argument("--autostart-survey", action="store_true")
    ap.add_argument("--offline", action="store_true", help="kinematic drone, no Cyberwave")
    ap.add_argument("--real-go2", action="store_true", help="stream E Go2Controller on the sim")
    ap.add_argument("--real-voice", action="store_true", help="SarvamVoice (spends Sarvam credit)")
    ap.add_argument("--real-detector", action="store_true", help="stream B YOLO + markers")
    ap.add_argument(
        "--stop-file",
        type=Path,
        default=None,
        help="keep running after the survey until this file exists (or the sim ends)",
    )
    ap.add_argument("--serve", action="store_true", help="also serve /ws and /api/state")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--every", type=float, default=5.0, help="snapshot log period (s)")
    ap.add_argument("--linger", type=float, default=3.0, help="seconds to keep running after")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "tests/evidence/V1-A")
    args = ap.parse_args(argv)
    configure_logging(get_settings().log_level)
    if not 0 < args.duration <= MAX_DURATION_S:
        log_event(log, "bad_duration", level=40, duration=args.duration, max=MAX_DURATION_S)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
