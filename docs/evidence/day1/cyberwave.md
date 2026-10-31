# Day-one Cyberwave live checks (RESEARCH.md §5 items 1–4)

**Run:** 2026-10-31 08:04–08:15 UTC · **By:** sim-engineer agent · **SDK:** `cyberwave==0.7.4` (Python 3.11 venv; numpy 2.4.6, opencv-python-headless 5.0.0.93, trimesh 5.2.0, paho-mqtt 2.1.0)
**Mode:** only `cw.affect("simulation", environment_id=env, duration=120)`; every run stopped in `finally` via `cw.environments.simulations.get_active(env).stop()`. MQTT capped with `CYBERWAVE_MQTT_CONNECT_TIMEOUT=3`, `CYBERWAVE_MQTT_CONNECT_ATTEMPTS=1` (8883 is blocked here).
**Billed simulated time:** about 156 s in total (run 1: about 24 s until the worker failed; run 2: 50 s; run 3: 82 s), at most 0.026 credits. The rubble start was refused before any instance launched.
**Scratch scripts and raw JSON/logs/JPEGs:** `/tmp/claude-0/-home-user-discord-bitcoin-bot/d1ac0a1f-06e2-582a-a01f-580701a8430d/scratchpad/day1cw/` (`a_setup.py`, `b_sim.py`, `c_go2.py`, `d0_glb_upload.py`, `e_run.py`, `out/`). Images not committed.

## IDs (safe to record)
| Thing | UUID |
|---|---|
| Workspace "aditya's Workspace" | `0a003362-ee60-4152-aeb7-05783591e311` |
| Project "skypaw" (created, account had none) | `402fe32e-382f-403a-8a69-6cabf96150b2` |
| Environment "skypaw-day1" | `7a2c46f0-aa23-4578-99a9-351831b4099c` |
| Go2 twin (`LocomoteJointCameraTwin`) | `16bf75cf-05c0-4db8-af8f-f7aa3c3d52ef` at editor (0,0,0) |
| px4vision twin, first (`FlyingCameraTwin`), deleted for isolation | `781b608c-37d7-43d7-9b21-cab30e3234fc` |
| px4vision twin, re-added at the end at editor (0,4,0) | `b359a313-6c5b-4296-af64-834accaa7983` |
| Rubble asset "skypaw-day1-rubble" (no GLB attached) | `aa69df7b-160a-4f59-8fb8-342c720413ce` |
| Rubble twin (deleted, it blocks MuJoCo) | `c790bc37-4703-4fef-8f23-877c79cf84b9` |
| Sim runs | `ac24896a-…` failed · `98f82732-…` Go2-only · `20095981-…` Go2-only, ready-wait |

**Environment state left behind:** Go2 + px4vision (the SPEC composition). **In this state MuJoCo will not start** (see check 1). No simulation is active.

## Summary
| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Go2 + px4vision → `affect("simulation")` | **FAIL.** The sim worker crashes at startup on the px4vision depth camera. | `error_message`: `Simulation worker exited with code 1 (failed during startup — failed to initialize simulator: Missing required parameter 'color_mode' in camera sensor 'front_depth_camera' on twin` (server text, truncated as shown). Before that, `affect()` raised `CyberwaveError: Simulation ac24896a-… did not become active (status: stopped)` after 1.3 s (SDK race, D-3). Preflight had said `eligible: true, blocking_assets: []`. |
| 1b | Go2 only → `affect("simulation")` | **PASS.** | `affect` returned after 3.0–3.1 s; status `running` at +3.2 s; **`simulation_ready: true` only at +49.4 s**. |
| 2 | Drone frames (`get_frame("numpy")`, `sensor_id="front_color_camera"`, `"front_depth_camera"`) | **NOT OBTAINABLE.** No px4vision sim runs. | All returned `None` while the doomed run was listed `running`. `drone.camera["front_depth_camera"].get_frame("numpy")` tried to connect to MQTT and raised `TimeoutError: timed out`. After stop: `SimulationNotRunningError`. |
| 2 | Go2 frames (`go2.camera.get_frame("numpy")`, `sensor_id="front_camera"`, `go2.get_frame("numpy")`) | **PASS, with a caveat.** | `(360, 640, 3) uint8` from all three. The first non-None frame (+18.2 s after `running`) is a **solid blue placeholder** (mean 85.0). By about +24 s it is a real MuJoCo render: dark grey ground plane, black sky, horizon at row about 180 and slightly tilted (171 left / 190 right), nothing else in view. Resolution is 640×360, not the catalog's 1280×720. Calls take 0.2–0.6 s. |
| 3 | `drone.takeoff(altitude=6)` | **FAIL** (transport). | Publishes over MQTT only. It raised `TimeoutError: timed out` after the socket-level connect to `mqtt.cyberwave.com:8883` failed. Before that it created a `controller_state` alert and assigned controller "Python SDK" via REST. REST drone position stayed `[0,4,0]` for 15 s. (No running sim either.) |
| 3 | `drone.pose.get()` / `go2.pose.get()` | **FAIL** (MQTT-only). | `TimeoutError: timed out` from `_ensure_pose_listeners` (MQTT connect). |
| 3 | `go2.navigation.goto([2.0,0.0,0.0])` before ready (+19 s after `running`) | **No-op, but no exception.** | It returned the dict `{"detail": "Simulation is not ready to receive navigation commands. Wait for the simulator to finish loading, then retry."}`. The SDK does not raise on this. |
| 3 | `go2.navigation.goto([2.0,0.0,0.0])` after `simulation_ready` | **Accepted, but the robot did not move in 30 s.** | Return: `{'action_id': '1d8e0081-…', 'status': 'queued', 'message': 'Navigation command queued', 'plan': None, metadata: {runtime_target: 'simulation', runtime_source_type: 'sim_tele', constraints.requires_obstacle_avoidance: True, planning_scene.obstacle_count: 0, …}}`. `GET /api/v1/twins/{uuid}/actions/{action_id}` still shows `queued`, "Navigation command received by the navigation bridge", acknowledged 0.8 s after send. Frames at t=0/5/10/15/20 s are identical (horizon row unchanged, mean abs pixel diff 0.03–0.07, no gait bobbing). `runtime-state` shows `navigation_lifecycle.state: idle`. |
| 3 | REST pose fallback | **Does not track the sim.** | `cw.twins.get(uuid)` returns a Twin wrapper (no `position_*`). `cw.twins.get_raw(uuid).position_*` works, but it is the **editor** pose: it stayed `[0,0,0]` through the whole run. `GET /twins/{uuid}/live-position` → 404 "not GPS-positioned". |
| 4 | GLB: generate → `cw.assets.create` → `cw.assets.upload_glb` | **Upload FAILS client-side (SDK bug).** | 1,984-byte, 3-box GLB built with trimesh. `assets.create(name="skypaw-day1-rubble")` OK (2.6 s). `upload_glb` raised `CyberwaveAPIError: Failed to upload glb for asset …: 4 validation errors for DefaultApi.src_app_api_assets_upload_glb file.bytes Input should be a valid bytes [input_type=BufferedReader] … file.tuple[str, bytes].0 …`. No request was sent. The new asset reads `supported_simulation_backends: ['playground']`, `build_status: 'pending'`, `glb_file: None`. |
| 4 | `cw.twin(asset.uuid, environment_id=env)` + sim start | **Start REFUSED, naming the asset.** | The twin was created (`Twin` class, 0.8 s). `affect` raised `CyberwaveError: Failed to start simulation for environment 7a2c46f0-…: Mujoco simulation is not enabled for: skypaw-day1-rubble skypaw-day1. (HTTP 400)`. Preflight just before still said `eligible: true, blocking_assets: []`. |

Time to first frame: **not available for the drone**. For the Go2: first non-None frame at +18.2 s after `running` (+21.5 s after the `affect` call), and that frame is a placeholder. First rendered frame at about +24 s. A sim usable for navigation needs about 50 s.

## Verified calls (work in 0.7.4)
- `cw.workspaces.list()`, `cw.projects.list()`, `cw.projects.create(name, workspace_id=)`, `cw.environments.list()` (also returns environments outside our project; match on name), `cw.environments.create(name=, project_id=, description=)`.
- `cw.assets.get_by_registry_id("unitree/go2" | "holybro/px4vision")` → both `supported_simulation_backends: ['playground','mujoco']`. Sensors: Go2 `front_camera` rgb + `lidar_4d`; px4vision `front_color_camera` rgb + `front_depth_camera` depth.
- `cw.twin(slug, environment_id=)` is idempotent: it reuses an existing twin of the same asset in the environment. `cw.twin(twin_id=uuid)` fetches. `twin.edit_position(x,y,z)` returns `None` and is reflected in `cw.twins.get_raw(uuid).position_*`. `cw.twins.delete(uuid)` works.
- `cw.environments.simulations.preflight(env, backend="mujoco")` → `{enabled, eligible, available_backends: ['mujoco','mujoco_warp'], blocking_assets, reasons, bundle}`. **Not reliable for MuJoCo:** it said eligible in both failing cases.
- `cw.environments.simulations.list(env)` raw entries carry `status`, **`simulation_ready`**, `error_message`, `total_duration_s`, `stream_profile`. `…_get_detail(env, sim_id)` (private) returns the failure reason after a run ends.
- `cw.affect("simulation", environment_id=env, duration=120)` returns the client, not the `Simulation`. Get the handle with `get_active(env)`. `sim.stop()` worked each time.
- `twin.camera.get_frame("numpy")` (cloud/REST) works on the Go2 without MQTT.
- `twin.navigation.goto([x,y,z])` is REST (`POST /api/v1/twins/{uuid}/navigation`) and returns the server JSON as a dict, including error bodies.
- REST GET `/api/v1/twins/{uuid}/actions/{action_id}` (action status, no MQTT needed) and `/api/v1/twins/{uuid}/runtime-state` (`navigation_lifecycle`) work.

## Discrepancies with docs/RESEARCH.md
1. **§0 D1 / §1.2: `holybro/px4vision` is not actually MuJoCo-runnable.** It is catalog-marked `mujoco` and preflight-eligible, but the worker fails with `Missing required parameter 'color_mode' in camera sensor 'front_depth_camera'`. This blocks every sim in an environment that contains it, so the recommended drone fails check 1. Needs a Cyberwave fix, or an alternate drone (`skydio/skydio-x2` has no depth sensor and is worth testing next), or a per-twin sensor override. Escalate: this is a SPEC-level decision.
2. **§1.6 `cw.environments.create(name, project_id=None)`** raises `CyberwaveAPIError: Failed to create environment: list index out of range` when the account has no projects. The SDK does `list_projects()[0]` before its own fallback. Fix: pass `project_id` (create a project via `cw.projects.create(name, workspace_id=)` first).
3. **§1.3 `affect("simulation")` "blocks until running":** `Simulation.refresh()` maps "neither listed nor detail-readable" to `stopped`. Right after start that raced and raised `did not become active (status: stopped)` after 1.3 s on run 1. Runs 2–3 did not race.
4. **§1.3 / §1.5: "running" ≠ ready.** `status` becomes `running` about 3 s after start, but `simulation_ready` turns true about 49 s later. Navigation before that is rejected with a `detail` body, and `goto` does not raise on it. Frames during that window are `None`, then a blue placeholder. Gate on `simulation_ready`, not `wait_until_active()`.
5. **§1.3 Flight in sim:** `takeoff/land/hover/…` are allowed in sim but are **MQTT publish only**. With 8883 blocked they cannot be sent from here at all. D2 understates this: it is not only telemetry, all drone flight commands need MQTT.
6. **§1.5 / D2: "poll the twin pose via REST"** does not work with the twin GET. `position_*` is the editor pose and does not follow the sim. `pose.get()` / `get_pose()` are MQTT-only. `/live-position` is GPS-only. No REST source of the simulated pose was found. Untested candidates: `GET /api/v1/twins/{uuid}/state`, `/telemetry`, `/joint_states`, `GET /api/v1/events/live`.
7. **§1.5 `goto` without a map (D5):** in an empty scene, `goto` was acknowledged by the "navigation bridge" but stayed `queued` with no motion for 30 s. The payload sets `requires_obstacle_avoidance: True`. Likely related to the missing occupancy map (D5); the cause is unconfirmed.
8. **§1.6 `cw.assets.upload_glb(asset_uuid, path)`** is broken in 0.7.4 for files ≤32 MB. `_upload_glb_direct` passes an open file object, and the generated client only accepts `bytes | str | (filename, bytes)`. It fails client-side with a pydantic `ValidationError`. Not worked around. The likely fix is to pass `("rubble.glb", path.read_bytes())` to `cw.api.src_app_api_assets_upload_glb(asset_id, file=...)`; this is untested and needs approval.
9. **§1.6 / D6:** a user-created asset defaults to `supported_simulation_backends: ['playground']`, and a twin of it makes the MuJoCo start fail with HTTP 400 `Mujoco simulation is not enabled for: <twin name>`. So any custom rubble must first be made MuJoCo-enabled (the "Prepare simulation" flow, `cw_prepare_simulation_asset`, or `POST /assets/{uuid}/sync-simulation-backends`; untested).
10. **§1.2 Go2 camera** renders 640×360 in sim, not the catalog's 1280×720.
11. **§1.4** `get_frame` raises `SimulationNotRunningError` when nothing is listed. While a run is listed `running` but not ready, it returns `None` silently and does not raise `SimulationLevelError`. `twin.camera["<sensor>"]` (indexing) tried to connect to MQTT in sim mode.
