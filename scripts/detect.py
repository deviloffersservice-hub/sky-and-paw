#!/usr/bin/env python3
"""Sky & Paw detection over captured frames (stream B), optionally publish to Cyberwave.

    scripts/detect.py --frames <dir> [--map-png] [--out <dir>] [--no-yolo]
    scripts/detect.py --frames <dir> --publish --env skypaw-v1-dev-b
    scripts/detect.py --live --env skypaw-v1-dev-b --duration 240 --out tests/evidence/V1-B

Frames dir: either ``mission.json`` (``{"frames": [{frame_id, path, t, sensor_id, position,
rotation, intrinsics}, ...]}``, paths relative to the dir) or plain ``*.jpg`` with a sidecar
``<stem>.json`` holding the same keys. ``position`` is world ``[x, y, z]`` (or ``{x,y,z}``);
``rotation`` is ``[w, x, y, z]`` (or the MQTT ``{w,x,y,z}`` dict).

Output (``--out``, default the frames dir): ``detections.json`` (candidates, per-frame boxes,
hits, latency, truth distances) and, with ``--map-png``, ``map.png``: rubble, footprint,
frame footprints, truth (hollow rings) and candidate pins coloured by suggested triage
(red immediate, amber delayed, grey expectant, blue unknown). Prints a candidate table.

``--live``: ensure the victim markers, put the drone's editor pose at z = 6 m above the
footprint centre, start MuJoCo through ``run_simulation`` (``simulation_affect``; always
stopped in ``finally``), capture ``camera_chasis_bottom`` with the MQTT pose before and after,
save ``live_<n>.jpg`` + sidecar JSON into ``--out``, then run the offline pipeline on them.
The drone is put back at its site.yaml spawn afterwards.
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from skypaw.config import get_settings  # noqa: E402
from skypaw.logging import configure_logging, get_logger, log_event  # noqa: E402
from skypaw.mission.ports import Candidate, Frame  # noqa: E402
from skypaw.perception.detector import CompositeDetector  # noqa: E402
from skypaw.perception.pipeline import DetectionPipeline, truth_errors  # noqa: E402
from skypaw.scene.site import Site, load_site  # noqa: E402
from skypaw.sensors.synthetic import SyntheticSensors  # noqa: E402

log = get_logger("skypaw.scripts.detect")
TRIAGE_BGR = {
    "immediate": (40, 40, 220),
    "delayed": (0, 170, 255),
    "expectant": (150, 150, 150),
    "unknown": (200, 140, 40),
}
DEFAULT_INTRINSICS = {"width": 640, "height": 360, "fovy": 58.72}


# --- frames ----------------------------------------------------------------------------------
def _frame_from(raw: dict[str, Any], base: Path, default_id: str) -> Frame:
    path = Path(raw.get("path") or raw.get("jpeg_path") or f"{default_id}.jpg")
    if not path.is_absolute():
        path = base / path
    return Frame(
        frame_id=str(raw.get("frame_id") or default_id),
        path=str(path),
        t=float(raw.get("t", 0.0)),
        sensor_id=str(raw.get("sensor_id", "camera_chasis_bottom")),
        position=raw["position"],
        rotation=raw["rotation"],
        intrinsics=raw.get("intrinsics") or dict(DEFAULT_INTRINSICS),
    )


def load_frames(frames_dir: Path) -> list[Frame]:
    """Frames from ``mission.json`` or ``*.jpg`` + sidecar ``.json``, in time order."""
    index = frames_dir / "mission.json"
    if index.exists():
        data = json.loads(index.read_text(encoding="utf-8"))
        rows = data["frames"] if isinstance(data, dict) else data
        frames = [_frame_from(r, frames_dir, f"f_{i:06d}") for i, r in enumerate(rows)]
    else:
        frames = []
        for jpg in sorted(frames_dir.glob("*.jpg")):
            side = jpg.with_suffix(".json")
            if not side.exists():
                continue
            raw = json.loads(side.read_text(encoding="utf-8"))
            raw.setdefault("path", jpg.name)
            frames.append(_frame_from(raw, frames_dir, jpg.stem))
    return sorted(frames, key=lambda f: f.t)


# --- map ---------------------------------------------------------------------------------------
def render_map(
    site: Site,
    cands: list[Candidate],
    sensors: SyntheticSensors,
    footprints: list[list[list[float]]],
    out: Path,
    px_per_m: float = 30.0,
) -> None:
    """Top-down world map PNG (X right, Y up)."""
    g = site.grid
    x0, x1 = g["x0"], g["x0"] + g["w"] * g["cell"]
    y0, y1 = -(g["z0"] + g["h"] * g["cell"]), -g["z0"]  # world Y = -site z
    w, h = int((x1 - x0) * px_per_m), int((y1 - y0) * px_per_m)
    img = np.full((h + 40, w, 3), 245, np.uint8)

    def px(x: float, y: float) -> tuple[int, int]:
        return int((x - x0) * px_per_m), int((y1 - y) * px_per_m)

    fp = site.footprint
    cv2.rectangle(
        img, px(fp["x_min"], -fp["z_min"]), px(fp["x_max"], -fp["z_max"]), (60, 60, 60), 1
    )
    for b in site.rubble:
        c, s = math.cos(b.yaw), math.sin(b.yaw)
        pts = []
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            lx, ly = dx * b.sx / 2, dy * b.sz / 2
            pts.append(px(b.x + c * lx - s * ly, -b.z + s * lx + c * ly))
        cv2.fillPoly(img, [np.array(pts)], (205, 205, 205))
        cv2.polylines(img, [np.array(pts)], True, (150, 150, 150), 1)
    for poly in footprints:
        if len(poly) >= 3:
            cv2.polylines(img, [np.array([px(*p) for p in poly])], True, (220, 160, 90), 1)
    for t in sensors.truths:
        cv2.circle(img, px(t.world[0], t.world[1]), 11, (0, 0, 0), 1)
        cv2.putText(
            img,
            t.id,
            (px(*t.world[:2])[0] + 12, px(*t.world[:2])[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
    for c in cands:
        p = px(c.position[0], c.position[1])
        cv2.circle(img, p, 7, TRIAGE_BGR.get(c.suggested_triage, (0, 0, 0)), -1)
        cv2.circle(img, p, 7, (0, 0, 0), 1)
        cv2.putText(
            img,
            f"{c.victim_id} {c.fused:.2f}",
            (p[0] + 9, p[1] + 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (30, 30, 30),
            1,
            cv2.LINE_AA,
        )
    x = 8
    for name, col in TRIAGE_BGR.items():
        cv2.circle(img, (x + 6, h + 20), 6, col, -1)
        cv2.putText(
            img, name, (x + 16, h + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA
        )
        x += 120
    cv2.putText(
        img,
        "ring = truth; dT/radar synthetic",
        (x, h + 25),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        (80, 80, 80),
        1,
        cv2.LINE_AA,
    )
    cv2.imwrite(str(out), img)


def annotate(image_path: str, boxes: list[dict[str, Any]], out: Path) -> None:
    img = cv2.imread(image_path)
    if img is None:
        return
    for b in boxes:
        x1, y1, x2, y2 = (int(v) for v in b["box"])
        col = (0, 0, 255) if b["person"] else (255, 120, 0)
        cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
        cv2.putText(
            img,
            f"{b['label']} {b['conf']:.2f}",
            (x1, max(10, y1 - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            col,
            1,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(out), img)


def print_table(cands: list[Candidate], errs: dict[str, float | None]) -> None:
    head = (
        f"{'id':6} {'x':>7} {'y':>7} {'rgb':>5} {'src':7} {'dT':>5} {'radar':12} "
        f"{'fused':>6} {'life':5} {'triage':10} {'err_m':>6}"
    )
    print(head)
    print("-" * len(head))
    for c in cands:
        ev = c.evidence
        radar = (
            f"{ev['radar']['bpm']} {ev['radar']['snr']}"
            if ev.get("radar")
            else ("negative" if ev.get("radar_negative") else "-")
        )
        rgb = f"{ev['rgb']:.2f}" if ev.get("rgb") is not None else "-"
        dt = f"{ev['thermal']:.1f}" if ev.get("thermal") is not None else "-"
        err = errs.get(c.victim_id)
        print(
            f"{c.victim_id:6} {c.position[0]:7.2f} {c.position[1]:7.2f} {rgb:>5} "
            f"{c.source.get('rgb', '-'):7} {dt:>5} {radar:12} {c.fused:6.3f} "
            f"{'yes' if c.life_sign else 'no':5} {c.suggested_triage:10} "
            f"{'-' if err is None else f'{err:.2f}':>6}"
        )


def run_offline(args: argparse.Namespace, frames_dir: Path, out_dir: Path) -> dict[str, Any]:
    settings = get_settings()
    site = load_site()
    sensors = SyntheticSensors(seed=args.seed)
    detector = CompositeDetector.from_settings(settings.data_dir, use_yolo=not args.no_yolo)
    pipe = DetectionPipeline(detector, sensors)
    frames = load_frames(frames_dir)
    if not frames:
        raise SystemExit(f"no frames with poses in {frames_dir}")
    cands: list[Candidate] = []
    for f in frames:
        cands = pipe.process(f)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = pipe.report()
    errs = truth_errors(cands, sensors)
    report.update(
        {
            "frames_dir": str(frames_dir),
            "yolo_loaded": detector.yolo is not None,
            "truth_distance_m": errs,
            "seed": args.seed,
            "note": "thermal/radar are synthetic (SPEC 3.2); marker boxes stand in for people",
        }
    )
    for rec, f in zip(pipe.records, frames, strict=True):
        if rec.boxes:
            annotate(f.path, rec.boxes, out_dir / f"{f.frame_id}_boxes.jpg")
    if args.map_png:
        render_map(site, cands, sensors, [r.footprint for r in pipe.records], out_dir / "map.png")
    if args.publish:
        report["publish"] = publish(args.env, cands)
    (out_dir / "detections.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print_table(cands, errs)
    lat = report["latency_ms"]
    print(
        f"\nframes {lat['frames']}  mean {lat['mean']} ms  max {lat['max']} ms  "
        f"fired {[r.fired for r in pipe.records]}"
    )
    return report


def publish(env_name: str, cands: list[Candidate]) -> dict[str, Any]:
    from skypaw.perception.publish import publish_alerts, publish_waypoints
    from skypaw.sim.client import get_client
    from skypaw.sim.scene import DRONE_SLUG, TWIN_NAMES

    cw = get_client()
    env_id = str(next(e for e in cw.environments.list() if e.name == env_name).uuid)
    wps = publish_waypoints(cw, env_id, cands)
    twins = {t.name: str(t.uuid) for t in cw.twins.list(environment_id=env_id)}
    drone = cw.twin(twin_id=twins[TWIN_NAMES["drone"]], asset_key=DRONE_SLUG)
    alerts = publish_alerts(drone, env_id, cands, wps)
    return {"env_id": env_id, "waypoints": wps, "alerts": alerts}


# --- live ------------------------------------------------------------------------------------
def run_live(args: argparse.Namespace, out_dir: Path) -> int:
    from skypaw.scene.markers import ensure_markers
    from skypaw.scene.site import SitePose, world_to_site
    from skypaw.sim.client import get_client
    from skypaw.sim.frames import frame_stats, save_jpeg, wait_for_frame
    from skypaw.sim.mqtt_ws import SceneMqtt
    from skypaw.sim.scene import DRONE_SLUG, TWIN_NAMES, SceneContext, place
    from skypaw.sim.sim_run import SimRun, preflight, run_simulation, stop_active

    settings = get_settings()
    site = load_site()
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = site.footprint
    cx, cy = (fp["x_min"] + fp["x_max"]) / 2, -(fp["z_min"] + fp["z_max"]) / 2 + 0.0
    report: dict[str, Any] = {"target_world": [cx, cy, args.alt], "yaw": args.yaw, "captures": []}
    cw = get_client(settings)
    ctx = SceneContext(cw)
    env_id = str(next(e for e in cw.environments.list() if e.name == args.env).uuid)
    report["markers"] = ensure_markers(cw, env_id, site)
    twins = {t.name: str(t.uuid) for t in cw.twins.list(environment_id=env_id)}
    drone_id = twins[TWIN_NAMES["drone"]]
    over = SitePose(world_to_site(cx, cy, args.alt), yaw=args.yaw)
    place(ctx, env_id, drone_id, TWIN_NAMES["drone"], over)
    mqtt = SceneMqtt(settings)
    code = 0

    def on_affected(run: SimRun) -> None:
        report["mqtt_connected"] = mqtt.connect(affected=True, twin_uuids=[drone_id])

    def pose() -> dict[str, Any]:
        st = mqtt.cache.latest(drone_id) or {}
        return {k: st.get(k) for k in ("position", "rotation")}

    try:
        stop_active(cw, env_id, reason="startup")
        preflight(cw, env_id)
        with run_simulation(cw, env_id, args.duration, on_affected=on_affected) as run:
            drone = cw.twin(twin_id=drone_id, asset_key=DRONE_SLUG)
            t_end = time.monotonic() + 15
            while time.monotonic() < t_end and not pose()["position"]:
                time.sleep(0.25)
            for n in range(args.captures):
                before = pose()
                res = wait_for_frame(drone, "camera_chasis_bottom", timeout_s=20)
                after = pose()
                cap: dict[str, Any] = {
                    "n": n,
                    "ok": res.ok,
                    "pose_before": before,
                    "pose_after": after,
                }
                if res.frame is not None and after["position"] and after["rotation"]:
                    name = f"live_{n}"
                    save_jpeg(res.frame, out_dir / f"{name}.jpg")
                    side = {
                        "frame_id": name,
                        "t": round(time.monotonic() - (run.t_ready or 0.0), 2),
                        "sensor_id": "camera_chasis_bottom",
                        "position": [after["position"][k] for k in "xyz"],
                        "rotation": [after["rotation"][k] for k in "wxyz"],
                        "intrinsics": dict(DEFAULT_INTRINSICS),
                        "pose_before": before,
                        "stats": frame_stats(res.frame).as_dict(),
                    }
                    (out_dir / f"{name}.json").write_text(json.dumps(side, indent=2) + "\n")
                else:
                    code = 5
                report["captures"].append(cap)
                time.sleep(args.capture_gap)
            report["run"] = run.as_dict()
        report["run"] = run.as_dict()
    except Exception as exc:  # noqa: BLE001
        log.exception("detect_live_error")
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        mqtt.close()
        place(ctx, env_id, drone_id, TWIN_NAMES["drone"], site.spawn["drone"])
        (out_dir / "live-run.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        log_event(log, "detect_live_end", exit_code=code)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sky & Paw detection over frames.")
    ap.add_argument("--frames", type=Path, help="frames dir (mission.json or jpg + .json)")
    ap.add_argument("--out", type=Path, help="output dir (default: the frames dir)")
    ap.add_argument("--map-png", action="store_true")
    ap.add_argument("--publish", action="store_true", help="write waypoints + alerts")
    ap.add_argument("--env", default=get_settings().cw_environment_name)
    ap.add_argument("--no-yolo", action="store_true", help="colour markers only")
    ap.add_argument("--seed", type=int, default=0, help="synthetic sensor noise seed")
    ap.add_argument("--live", action="store_true", help="capture live frames first (billable)")
    ap.add_argument("--duration", type=float, default=240.0)
    ap.add_argument("--alt", type=float, default=6.0)
    ap.add_argument("--yaw", type=float, default=0.0, help="drone spawn yaw (rad, about +Z)")
    ap.add_argument("--captures", type=int, default=2)
    ap.add_argument("--capture-gap", type=float, default=3.0)
    args = ap.parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.live:
        out = args.out or REPO_ROOT / "tests/evidence/V1-B"
        code = run_live(args, out)
        if any(out.glob("live_*.json")):
            run_offline(args, out, out)
        return code
    if args.frames is None:
        ap.error("--frames is required unless --live")
    run_offline(args, args.frames, args.out or args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
