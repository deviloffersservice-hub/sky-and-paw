# M2 — Drone mission: takeoff, survey pattern, camera down, frame capture

**Owner:** sim-engineer · **Branch:** `feat/m2-drone` from `main` (after M1 merges) · **Reviewer:** reviewer · **QA:** qa-engineer (live, ≤ 0.2 credits)
**Inputs:** SPEC.md v1.2 §6.3 (survey geometry: x −8…8, rows z −5…5 step 2.5, altitude 6 m, 2.2 m/s), §4 Drone model, docs/RESEARCH.md §6 (Skydio X2: spawn z ≥ 0.5, cameras `camera_chasis_bottom` (down) and `payload_color_camera` (forward), `takeoff` works over MQTT-WSS, no gimbal; pose from MQTT `position`/`rotation`), M1 deliverables (`skypaw.sim.scene`, `mqtt_ws`, `readiness`, `frames`, `sim_run`).

## Goal
A `make drone-mission` command that, in a running MuJoCo scene from M1, takes the drone off to 6 m, flies the five-pass lawnmower over the footprint while keeping the downward camera on the ground, captures a geo-tagged frame every ≤ 2.5 m of travel (pose from MQTT at capture time, intrinsics from the catalog), marks grid cells as *seen* exactly as SPEC §6.3 describes (radius 2.8 m), returns to a hold point (0, 9, 0), and lands on demand. Every frame is saved with its pose so M3 can run detection offline against real captures. **Demo:** the console log of the mission, a contact sheet of ~40 frames with their world positions, and a rendered "seen" grid (PNG) showing ≥ 95% footprint coverage.

## Deliverables
```
backend/skypaw/mission/grid.py         # OccupancyGrid: 0.5 m cells over x −13…13, z −9…9; classes unmapped/passable/difficult/blocked from site.yaml rubble boxes (same rule as the mockup engine: height ≥ 0.6 → blocked, else difficult; 1-cell difficult buffer around blocked); seen/verified masks; reveal(x, z, r); mapped_pct(); to_png()
backend/skypaw/drone/controller.py     # DroneController(twin, pose_source): takeoff(alt), fly_to(x, z, alt, speed) as a closed-loop on MQTT pose using locomotion/flight velocity commands (or REST navigation if it proves to execute for aerial twins: test both, keep the one that moves the twin, document which), hold(), land(); tolerances 0.4 m xy, 0.3 m z; timeouts; abort on pose staleness > 3 s
backend/skypaw/drone/survey.py         # survey_waypoints(site) → the 10 lawnmower corners + hold point; run_survey(controller, capture) with capture every ≤ 2.5 m of travel; emits SurveyProgress events {pct_mapped, frames, position}
backend/skypaw/drone/capture.py        # capture_frame(twin, sensor_id) → Frame{jpeg_path, t, position, rotation, sensor_id, intrinsics} ; intrinsics from assets_capabilities (fov 58.72°, 640×360 in sim); saved under DATA_DIR/missions/<id>/frames/
backend/skypaw/mission/state.py        # MissionState dataclass (drone pose, grid, frames index, phase) + JSON serialisation for later WebSocket use
scripts/drone_mission.py               # CLI: --env skypaw-m2 --duration 300 --alt 6 --speed 2.2 --contact-sheet; prints progress lines; writes frames, grid.png, contact_sheet.png, mission.json
backend/tests/                         # grid unit tests against the mockup's known cells (V-01 under slab edge is difficult, staging passable, inside a wall blocked); survey waypoint order; controller state machine with a fake pose source; capture spacing logic
docs/RUNBOOK.md                        # section "Drone mission"
```

## Rules specific to M2
- Flight commands only after `simulation_ready`; the controller refuses to start if the MQTT pose for the drone is older than 3 s.
- The drone must never leave the box x ∈ [−12, 12], z ∈ [−8, 10], y ∈ [0, 8]; the controller clamps targets and aborts with `land()` if the pose leaves it.
- Capture only when `|velocity| < 2.5 m/s` and the pose timestamp is within 200 ms of the frame request; record both timestamps.
- The downward camera is body-fixed; keep the drone level (no commanded pitch) during captures. If the X2 cannot be commanded to a level hover, record the measured tilt and pass it into the frame's rotation.
- Frames are JPEG 640×360 as returned; do not upscale.
- Requested simulation time per run ≤ 300 s; the mission must complete in ≤ 180 s of flight at 2.2 m/s (≈ 90 m of path).

## Acceptance criteria
- M2-A1 Offline: grid classes match the mockup engine on 20 sampled cells; `mapped_pct()` of a fully revealed footprint is 100; unit tests green.
- M2-A2 Live: `takeoff(6)` reaches 6 ± 0.3 m within 20 s (MQTT pose), logged.
- M2-A3 Live: the survey visits all 10 corners within 0.4 m, in order, in ≤ 180 s; `grid.mapped_pct() ≥ 95` at the end; `grid.png` saved.
- M2-A4 Live: ≥ 35 frames captured, each with pose within 200 ms, non-black (mean > 5), 640×360; `contact_sheet.png` shows the rubble passing beneath on at least 5 frames.
- M2-A5 Live: the hold point is reached and held for 10 s within 0.5 m; `land()` brings y < 0.3 m.
- M2-A6 Abort path: with `--inject-pose-loss` (test flag that stops consuming MQTT), the controller aborts within 5 s, lands, and the sim is stopped in `finally`.
- M2-A7 `mission.json` validates against a JSON schema committed with it and contains frame count, path length, duration, mapped_pct, and the sensor used.
- M2-A8 Total requested simulation time across QA ≤ 15 min; credits logged.

## Test plan
- Unit: grid, survey order, controller state machine with a scripted fake pose source, capture gating.
- Live (QA): one full run on `skypaw-m2` with `--contact-sheet`; the A6 abort run; verify no active sims afterwards; collect `mission.json`, `grid.png`, `contact_sheet.png`, logs, 5 sample frames → tests/evidence/M2/REPORT.txt.
