#!/usr/bin/env python3
"""Sky & Paw M2 probe: an overhead camera frame of the rubble from the Skydio X2.

    scripts/overhead.py --mode flight --duration 120   # takeoff + closed-loop flight
    scripts/overhead.py --mode spawn --duration 60     # drone spawned at z=6 above the rubble

flight: takeoff(altitude=6) over MQTT, wait for z >= 5.5 (20 s), then fly to the footprint
centre with locomotion bursts (turn_left/turn_right + move_forward) closed-loop on MQTT pose,
tolerance 0.5 m, 25 s. spawn: set the drone's editor pose to (centre, z=6) before the sim
starts; the drone is put back at its site.yaml spawn afterwards. Both: capture
camera_chasis_bottom and payload_color_camera, and one Go2 front frame after
go2.locomotion.move_forward(0.4, duration=2). The simulation always stops in finally.
Evidence: tests/evidence/M2/dev/ (overhead_*.jpg, go2_front_moved.jpg, overhead-<mode>.json).
Exit codes: 0 ok, 1 error, 5 a step did not succeed (see the JSON).
"""

from __future__ import annotations

import argparse
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
from skypaw.logging import configure_logging, get_logger, log_event  # noqa: E402
from skypaw.scene.site import SitePose, load_site, world_to_site  # noqa: E402
from skypaw.sim.client import get_client  # noqa: E402
from skypaw.sim.frames import frame_stats, save_jpeg, wait_for_frame  # noqa: E402
from skypaw.sim.mqtt_ws import SceneMqtt  # noqa: E402
from skypaw.sim.scene import DRONE_SLUG, GO2_SLUG, TWIN_NAMES, SceneContext, place  # noqa: E402
from skypaw.sim.sim_run import SimRun, preflight, run_simulation, stop_active  # noqa: E402

log = get_logger("skypaw.scripts.overhead")
ALT = 6.0
TOL = 0.5


def yaw_of(q: dict[str, float]) -> float:
    """Yaw (rad, about world +Z) of a quaternion {x, y, z, w}."""
    x, y, z, w = (q.get(k, 0.0) for k in "xyzw")
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def pose(mqtt: SceneMqtt, uid: str) -> dict[str, Any]:
    return mqtt.cache.latest(uid) or {}


def wait_alt(mqtt: SceneMqtt, uid: str, z_min: float, timeout_s: float) -> float | None:
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        p = pose(mqtt, uid).get("position")
        if p and p["z"] >= z_min:
            return p["z"]
        time.sleep(0.25)
    return None


def fly_to(drone: Any, mqtt: SceneMqtt, uid: str, tx: float, ty: float, timeout_s: float) -> bool:
    """Closed-loop on MQTT pose: turn toward the target, then forward bursts."""
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        st = pose(mqtt, uid)
        p, q = st.get("position"), st.get("rotation")
        if not p or not q:
            time.sleep(0.25)
            continue
        dx, dy = tx - p["x"], ty - p["y"]
        dist = math.hypot(dx, dy)
        if dist <= TOL:
            return True
        err = (math.atan2(dy, dx) - yaw_of(q) + math.pi) % (2 * math.pi) - math.pi
        log_event(log, "fly_step", dist=round(dist, 2), heading_err=round(err, 2), z=p["z"])
        if abs(err) > 0.25:
            turn = drone.locomotion.turn_left if err > 0 else drone.locomotion.turn_right
            turn(0.6, duration=min(1.0, abs(err) / 0.6))
        else:
            drone.locomotion.move_forward(min(1.5, max(0.3, dist / 1.5)), duration=1.0)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Overhead rubble frame from the drone.")
    ap.add_argument("--mode", choices=("flight", "spawn"), default="flight")
    ap.add_argument("--env", default=get_settings().cw_environment_name)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "tests/evidence/M2/dev")
    args = ap.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    ev: Path = args.evidence_dir
    ev.mkdir(parents=True, exist_ok=True)
    site = load_site()
    fp = site.footprint
    centre_site = ((fp["x_min"] + fp["x_max"]) / 2, (fp["z_min"] + fp["z_max"]) / 2)
    cx, cy = centre_site[0], -centre_site[1] + 0.0  # world (X, Y) of the footprint centre
    report: dict[str, Any] = {"mode": args.mode, "target_world": [cx, cy, ALT], "steps": {}}
    code = 0

    cw = get_client(settings)
    ctx = SceneContext(cw)
    env = next(e for e in cw.environments.list() if e.name == args.env)
    env_id = str(env.uuid)
    twins = {t.name: str(t.uuid) for t in cw.twins.list(environment_id=env_id)}
    drone_id, go2_id = twins[TWIN_NAMES["drone"]], twins[TWIN_NAMES["go2"]]
    rubble_id = twins.get(TWIN_NAMES["rubble"])
    if args.mode == "spawn":
        over = SitePose(pos=world_to_site(cx, cy, ALT), yaw=0.0)
        place(ctx, env_id, drone_id, TWIN_NAMES["drone"], over)
    mqtt = SceneMqtt(settings)

    def on_affected(run: SimRun) -> None:
        ids = [u for u in (drone_id, go2_id, rubble_id) if u]
        report["mqtt_connected"] = mqtt.connect(affected=True, twin_uuids=ids)

    try:
        stop_active(cw, env_id, reason="startup")
        preflight(cw, env_id)
        with run_simulation(cw, env_id, args.duration, on_affected=on_affected) as run:
            t_ready = time.monotonic()
            report["time_to_ready_s"] = run.time_to_ready_s
            drone = cw.twin(twin_id=drone_id, asset_key=DRONE_SLUG)
            go2 = cw.twin(twin_id=go2_id, asset_key=GO2_SLUG)
            if args.mode == "flight":
                t0 = time.monotonic()
                drone.takeoff(altitude=ALT)
                z = wait_alt(mqtt, drone_id, 1.0, 8)
                if z is None:  # first command after the policy auto-attach can be lost
                    drone.takeoff(altitude=ALT)
                z = wait_alt(mqtt, drone_id, ALT - 0.5, 20)
                report["steps"]["takeoff"] = {"z": z, "s": round(time.monotonic() - t0, 1)}
                t1 = time.monotonic()
                ok = z is not None and fly_to(drone, mqtt, drone_id, cx, cy, 25)
                report["steps"]["lateral"] = {"ok": ok, "s": round(time.monotonic() - t1, 1)}
                if not ok:
                    code = 5
            st = pose(mqtt, drone_id)
            report["pose_at_capture"] = {k: st.get(k) for k in ("position", "rotation")}
            for sensor, name in (
                ("camera_chasis_bottom", "overhead_bottom.jpg"),
                ("payload_color_camera", "overhead_payload.jpg"),
            ):
                res = wait_for_frame(drone, sensor, timeout_s=15)
                report["steps"][sensor] = {
                    "ok": res.ok,
                    "stats": res.stats.as_dict() if res.stats else None,
                    "since_ready_s": round(time.monotonic() - t_ready, 1),
                }
                if res.frame is not None:
                    save_jpeg(res.frame, ev / name)
                else:
                    code = 5
            go2.locomotion.move_forward(0.4, duration=2)
            time.sleep(1.0)
            frame = go2.camera.get_frame("numpy", sensor_id="front_camera")
            if frame is not None:
                save_jpeg(frame, ev / "go2_front_moved.jpg")
                report["steps"]["go2_front_moved"] = frame_stats(frame).as_dict()
            report["go2_pose_after_move"] = pose(mqtt, go2_id).get("position")
            report["run"] = run.as_dict()
        report["run"] = run.as_dict()
    except Exception as exc:  # noqa: BLE001
        log.exception("overhead_error")
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        mqtt.close()
        if args.mode == "spawn":
            place(ctx, env_id, drone_id, TWIN_NAMES["drone"], site.spawn["drone"])
        (ev / f"overhead-{args.mode}.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        log_event(log, "overhead_end", exit_code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
