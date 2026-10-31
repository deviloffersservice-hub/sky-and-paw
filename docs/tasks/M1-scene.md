# M1 — Scene: environment + rubble GLB + drone + Go2, by idempotent script

**Owner:** sim-engineer · **Branch:** `feat/m1-scene` from `main` (after T0 merges) · **Reviewer:** reviewer · **QA:** qa-engineer (live, ≤ 0.15 credits)
**Inputs:** SPEC.md v1.2 (§3.1, §4 Site/grid, §6.3 geometry constants), docs/RESEARCH.md §1 and **§6 (live results; these override §1 where they differ)**, docs/evidence/day1/cyberwave.md round 2, T0 scaffold (`skypaw.config`, `skypaw.sim.guard`, `skypaw.sim.simulation_affect`).

## Goal
One command, `make scene`, that creates or reuses everything the mission needs in Cyberwave and proves it works: a project, an environment, a MuJoCo-ready rubble GLB generated from the spec's layout, a Go2 twin, a Skydio X2 drone twin at z ≥ 0.5 m, persisted environment waypoints for staging and the drone survey grid, a MuJoCo simulation started through the guarded path, live pose over MQTT-over-WSS, one captured frame from each camera, and a clean stop. Running it twice changes nothing. **Demo:** `make scene` output plus the two saved frames and a Cyberwave dashboard view of the environment.

## Deliverables (files)
```
backend/skypaw/sim/
  client.py        # get_client(): Cyberwave() from settings; workspace/project lookup; never prints the key
  mqtt_ws.py       # paho Client subclass: transport="websockets", path "/", TLS, port 443, username/password from settings; connect AFTER affect; subscribe per-twin "#"; pose cache {twin_uuid: {position, rotation, t}} with a thread-safe getter and an async queue
  scene.py         # ensure_project(), ensure_environment(), ensure_asset_glb(), ensure_twin(), ensure_waypoints(), place(); all idempotent by name lookup; returns a SceneHandles dataclass with every uuid
  readiness.py     # wait_simulation_ready(cw, env_id, timeout_s=120) polling the simulations list for simulation_ready/error_message; handles the affect() race by retrying once
  frames.py        # capture(twin, sensor_id, fmt="numpy") with None handling (returns None while not ready) and save_jpeg()
  sim_run.py       # context manager run_simulation(duration_s): simulation_affect → wait_simulation_ready → yield → stop in finally (also on KeyboardInterrupt)
backend/skypaw/scene/
  site.yaml        # SPEC geometry as data: footprint 18×12 m, STAGING (-12.5, 7.5), RUBBLE list (x, z, sx, sy, sz, yaw) copied from the mockup engine, VICTIMS ground truth (V-01..V-03, D-01 with positions, truth sensors, language), SURVEY grid (x0 -8, x1 8, rows -5..5 step 2.5, alt 6, speed 2.2), drone spawn (-11.5, 0.6, 5.5), go2 spawn (-11, 0, 7.5)
  build_glb.py     # generates scene/rubble.glb from site.yaml with trimesh (pinned); boxes only; z-up→y-up conversion documented; deterministic; < 1 MB
scripts/scene.py   # CLI: `--project skypaw --env skypaw-m1 --duration 120 --capture --dry-run`; prints a table of handles; exit codes documented
Makefile           # `make scene` → scripts/scene.py --capture; `make scene-dry` → --dry-run (no API calls beyond GETs)
backend/tests/     # unit tests with the Cyberwave client mocked: idempotency (second call makes zero POSTs), guard on the affect path, mqtt_ws URL/options, build_glb determinism (hash), readiness state machine incl. error_message and timeout
tests/evidence/M1/ # QA: console log, handles.json (uuids only), drone_bottom.jpg, drone_payload.jpg, go2_front.jpg, mqtt_pose_samples.json (≥ 20 samples for each twin), timings
docs/RUNBOOK.md    # section "Scene": how to run, what it costs, how to reset (delete env), known Cyberwave quirks list
```

## Rules specific to M1 (in addition to the hard rules)
- `cw.affect` is called only through `skypaw.sim.simulation_affect` with the literal `"simulation"` and `duration ≤ settings.SIM_MAX_DURATION_S` (600). Default duration 120 s.
- Every simulation start is wrapped in `run_simulation()`; the `finally` stops it even on exceptions. The script also stops any sim already active on the environment at startup (`get_active`) and logs it.
- Drone spawn z = 0.6 (RESEARCH §6: black frames at z=0). Go2 spawn on the ground at staging.
- GLB upload uses the raw API call `cw.api.src_app_api_assets_upload_glb(uuid, file=(name, bytes))` because `assets.upload_glb` is broken in 0.7.4; wrap it as `upload_glb_bytes()` with a comment citing RESEARCH §6. Then PATCH the asset with `supported_simulation_backends=["playground","mujoco"]` and `fixed_base=True` via `src_app_api_assets_update_asset`; record whether `fixed_base` is accepted. Run `simulations.preflight` before starting and fail fast on blocking assets.
- MQTT: connect only after `affect`; subscribe to `cyberwave/twin/{uuid}/#` for each twin and `cyberwave/environment/{uuid}/#`; never cross-twin wildcards. If WSS on 443 fails, log and continue (frames still work over REST); do not fall back to port 8883 silently.
- Waypoints: create "staging-west" and "survey-01".."survey-10" (the lawnmower corners from site.yaml) with metadata `{"kind":"staging"|"survey","order":n}`; idempotent by name.
- Never write uuids of the user's account into docs except handles.json in evidence.
- Pin new deps: `trimesh==4.5.x`, `paho-mqtt` already present (record version), `pyyaml` present.

## Acceptance criteria
- M1-A1 `make scene-dry` makes no POST/PUT/PATCH calls (mocked client asserts) and prints the plan.
- M1-A2 First `make scene` creates project "skypaw" (or reuses), environment `skypaw-m1`, asset `skypaw-rubble` with `glb_file` set and `supported_simulation_backends` containing `mujoco`, twins `unitree/go2` and `skydio/skydio-x2`, 11 waypoints; second run performs **zero** create calls (verified by counting POSTs in the HTTP log) and prints the same handles.
- M1-A3 Preflight reports eligible with no blocking assets; MuJoCo starts; `simulation_ready` within 120 s; the script logs time-to-ready.
- M1-A4 Frames: `camera_chasis_bottom` and `payload_color_camera` of the drone and `front_camera` of the Go2 return non-black 640×360 arrays within 40 s of ready (mean pixel value > 5); JPEGs saved to evidence. The rubble is visible in at least one frame (QA eyeballs and notes it).
- M1-A5 MQTT over WSS 443 connects within 5 s after affect; ≥ 20 `position` samples for the Go2 and ≥ 20 for the drone are cached within 60 s of ready; pose cache getter returns the latest sample with a timestamp.
- M1-A6 Rubble stays put: after `go2.locomotion.move_forward(0.5, duration=2)` toward a rubble box (QA step), the rubble twin's MQTT position changes by < 0.05 m if `fixed_base=True` was accepted; otherwise the report records the actual displacement and flags `fixed_base` as unsupported.
- M1-A7 The simulation is stopped at the end (no active simulations on the environment) in all of: normal exit, exception injected by a test flag `--fail-after-ready`, and Ctrl-C (SIGINT sent by QA).
- M1-A8 Total requested simulation time across the full QA run ≤ 10 minutes; the script prints the requested duration and estimated credits (0.01/min) at start and end.
- M1-A9 Unit tests pass offline; `make test` green; ruff clean; no key material in any file or log (QA greps evidence).

## Test plan
- Unit (offline): all of M1-A1, idempotency with a mocked client, readiness state machine, mqtt_ws configuration, build_glb hash.
- Live (QA, with the real key): run `make scene` twice; collect handles.json, logs, frames, pose samples; run the A6 push test and the three A7 stop scenarios; verify no active sims via `simulations.list`; record credits before/after from the script's estimate.
- Evidence: tests/evidence/M1/REPORT.txt (QA writes .txt; Fable converts to REPORT.md) with each criterion PASS/FAIL and file references.
