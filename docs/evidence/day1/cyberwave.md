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

---

# Round 2 (checks A–E)

**Run:** 2026-10-31 08:19–08:35 UTC · **By:** sim-engineer agent · **SDK:** `cyberwave==0.7.4`, paho-mqtt 2.1.0, trimesh 5.2.0, plus `PySocks==1.7.1` (scratch venv only, for the HTTP-CONNECT probe).
**Mode:** only `cw.affect("simulation", environment_id=env, duration=120)`. Every run was stopped in `finally` (`get_active(env).stop()`), and no simulation is active in either environment.
**New environment:** "skypaw-day1b" `6efa2c7f-edd7-45ad-a071-2a0b3d1857f9` (project `402fe32e-…`). The round-1 environment "skypaw-day1" was not touched.
**Scratch:** the same directory as round 1. Scripts `r2_common.py`, `r2_sdk_ws.py`, `a_mqtt_ws.py`, `a2_mqtt_sub.py`, `a3_sdk_ws.py`, `a4_echo.py`, `a5_capture_check.py`, `b0_setup2.py`, `b1_sim.py`, `b2_sim.py`, `b3_sim.py`, `c2_free_probes.py`, `d1_glb.py`, `d2_enable_mujoco.py`, `d3_place.py`. Outputs are in `out/round2/`. Signed GLB URLs and the MQTT username hash are redacted there. The key never appears.

## IDs (round 2)
| Thing | UUID |
|---|---|
| Environment "skypaw-day1b" | `6efa2c7f-edd7-45ad-a071-2a0b3d1857f9` |
| Go2 twin (`LocomoteJointCameraTwin`), editor (0,0,0) | `7539f1bf-ad22-4dfb-a2b0-a2639e05660a` |
| Skydio X2 twin (`FlyingCameraTwin`), editor (0,4,0) in run R2-1, then **(0,4,0.5)** | `e6505df0-1be5-4af5-93f9-58631a9dd51f` |
| Rubble asset "skypaw-day1b-rubble" (GLB attached, MuJoCo-enabled) | `5652e9c1-5d9a-4371-8892-470a4827415e` |
| Rubble twin (`Twin`), editor (2,0,0) | `81a2a58a-3cb3-403c-8230-477797b8cb41` |
| Sim runs | R2-1 `2886da02-…` (Go2+X2) · R2-2 `e887311d-…` (Go2+X2, drone at z=0.5) · R2-3 `5fdefc7d-…` (Go2+X2+rubble) |

**Environment state left behind:** Go2 at (0,0,0), X2 at (0,4,0.5), rubble at (2,0,0). MuJoCo starts in this state.

## Summary
| # | Check | Result | Evidence |
|---|---|---|---|
| A | MQTT over WSS 443, paths `/mqtt`, `/`, `/ws` (raw paho, `transport="websockets"`, `tls_set()`, MQTTv5) | **PASS on path `/` only.** | `/`: `connect()` rc 0, **CONNACK Success in 0.39 s** with username `mqttcyb` and with the SDK-derived `cwh_<sha256(key)>`. `/mqtt` and `/ws`: `WebsocketConnectionError: WebSocket handshake error, connection not upgraded` (0.18–0.26 s). The second attempt through the explicit HTTP-CONNECT proxy (PySocks) also failed: `WebSocket handshake error` (0.42–0.46 s). curl upgrade probe: `/mqtt` 404, `/ws` 404, `/` **101**. |
| A | Subscribe | **Wildcards across twins are refused. Per-twin and per-environment topics are granted.** | `cyberwave/twin/+/position` and `cyberwave/twin/+/navigate/status` → SUBACK `Not authorized`. `cyberwave/twin/{uuid}/position`, `…/navigate/status`, `cyberwave/twin/{uuid}/#` and `cyberwave/environment/{uuid}/#` → `Granted QoS 0`. A pub/sub echo between two WSS clients works (QoS1 PUBACK `Success`). |
| A | SDK's own MQTT client on WSS | **Not configurable in 0.7.4. It works with a 10-line scratch patch.** | See "SDK transport" below. Patched: `cw.mqtt.connect()` connected in 1.84 s to `mqtt.cyberwave.com:443`, and SUBACK was granted. |
| B | Go2 + `skydio/skydio-x2` → `affect("simulation")` | **PASS.** MuJoCo starts. | `affect` returned in 2.97 / 3.88 / 2.98 s. `running` at +3.2 s. **`simulation_ready` at +53.6 s / +43.7 s / +54.0 s** (R2-1/2/3). Preflight `eligible: true`. |
| B | Drone frames, editor z=0 (R2-1) | **Black.** | `get_frame("numpy")` default, `sensor_id="payload_color_camera"`: `(360,640,3)` uint8, all zeros. `camera_chasis_bottom`: mean 0.49 (black, with a sliver at the bottom edge). While loading: `None` at +8/+15/+22 s, one uniform frame (mean 19.0, std 0.29) at +29 s, then zeros. |
| B | Drone frames, editor z=0.5 (R2-2) | **Bottom and payload cameras render.** The default camera is black. | `camera_chasis_bottom`: uniform grey ground at close range, mean 119.4, landing gear at the bottom edge. `payload_color_camera`: horizon view like the Go2's (black sky, grey ground, mean 18.8). Default sensor (= `camera_front_left`): all zeros. It probably looks up at the black sky. Resolution 640×360 for every sensor. Calls take 0.2–0.4 s. |
| B | `drone.takeoff(altitude=6)` (2 attempts) | **Works on attempt 2 (drone at z=0.5). Telemetry was not captured.** | R2-1 (z=0): returned `None` in 5.89 s. The SDK auto-attached the "Python SDK" teleop policy and warned "This command may have been lost while the robot was still setting up". Frames stayed black. R2-2 (policy already attached): returned `None` in 2.12 s. **The bottom camera at +10 s shows the ground far below, with the Go2 visible as a small figure** (`b2_drone_bottom_takeoff_plus10.jpg`); +10 s and +20 s are identical (hover). From geometry, the Go2 is 4 m away horizontally and about 35° off nadir, so altitude is about 5.7 m, consistent with 6 m. MQTT position during takeoff was **not recorded**: `cw.affect()` drops the SDK MQTT client and its subscriptions (see D-R2-3). The command went to `cyberwave/twin/{uuid}/command` `{"source_type":"sim_tele","command":"takeoff","data":{"altitude":6}}`. |
| C | `go2.policy` | **Exists** (`PolicyCapableMixin` → `TwinPolicyHandle`: `list`, `get`, `assign`, `unassign`, `attached`, `playground_actuations`, `ensure_attached`, `keyboard`). | The Go2 twin was created with "Quadruped Controller" `6518d067-…` (teleop) attached. `ensure_attached()` → `None` in 1.97 s, no change. It requires a **teleop** policy. Policies visible for the Go2 include "Go2 Autonomous Navigation" `6599182c-…` (navigation), "Navigation" `9501e1c3-…` (navigation), "Go2 MuJoCo Velocity Policy" `0b15200b-…` (mlmodel). |
| C | `go2.navigation.goto([2,0,0])` ×2, `follow_path([[1,0,0],[2,0,0]])` ×1 | **No motion. Status never leaves `queued`.** | goto 1 (+3 s after ready) → `queued`, action `8a037d4c-…`. goto 2 with `controller_policy_uuid="6599182c-…"` → `queued`, action `94610e0c-…`. follow_path → `queued`, action `51e29174-…`. `GET …/actions/{id}` every 2 s for 20 / 20 / 8 s: `queued`, "Navigation command received by the navigation bridge". `runtime-state` `metadata.navigation_lifecycle = {state: "idle", updated_at: 08:23:49Z}` (before the first goto), unchanged. Go2 frames before and after: mean abs diff 0.07. The payload still says `requires_obstacle_avoidance: true`, `obstacle_count: 0`. |
| C | `go2.locomotion.move_forward(0.5, duration=…)` (diagnostic, 2 attempts) | **Moves the Go2.** | R2-3, `duration=4.0`: 81 `command` messages at about 20 Hz, then `stop`. Go2 MQTT position went **(0.05, 0.01, 0.35) → (2.04, 0.08, 0.36)**, yaw +6°. The Go2 camera closes on the rubble (`b3_grid.jpg`). R2-2 (`duration=3.0`, not captured): horizon tilt changed 190→186 px. |
| C | REST simulated pose | **None found.** | `GET /twins/{uuid}/state` → **405** "Method not allowed" (the route is PATCH-only). `GET /telemetry?limit=5` → **400** "start and end query parameters are required."; with `start`/`end` → 200, but the items are recording and camera-segment events, not pose. `GET /joint_states` → 200, all 12 Go2 joints `position_source: "home"` (0, 0.9, −1.8), unchanged; drone empty. Simulation detail → `stream_profile` only. Twin `position_*` = editor pose. |
| C | MQTT simulated pose (R2-3, independent WSS capture client) | **PASS. Rich realtime state.** | From +23 s after `affect` (about 30 s before `simulation_ready`): `…/{drone,rubble}/position` and `/rotation` at about 19 Hz (`{"source_type":"sim","type":"position","position":{x,y,z},"simulation_time_s":…}`); `…/{all}/link_poses` and `/free_joint_state` (qpos/qvel) at about 19 Hz; Go2 `lidar` + `pointcloud` at about 8.5 Hz. **The Go2 `position`/`rotation` arrive only from +51 s** (session `go2_nav_…`, about 8 Hz, `frame: "world"`, `odometry_session_id`), together with `navigation/map_preview`. Also `telemetry`, `webrtc-offer/answer`, and `environment/{uuid}/telemetry-status`. 6,113 messages by ready. The drone holds (0,4,0.5) exactly: spawned drones do not fall. |
| D | Raw GLB upload | **PASS.** | `dir(cw.api)` has `src_app_api_assets_upload_glb` (`uuid`, `file: bytes | str | (str, bytes)`). `cw.api.src_app_api_assets_upload_glb(asset_id, file=("rubble.glb", data_bytes))` → `AssetSchema` in 3.02 s. **`glb_file` is set**: a signed URL on `storage.googleapis.com/cyberwave-downloads-cyberwave-os-production/media/assets/glb_files/<asset>.glb` (24 h expiry). |
| D | `POST /api/v1/assets/{uuid}/sync-simulation-backends` (`cw.api.src_app_api_assets_sync_simulation_backends(uuid)`, no body) | **No effect for custom assets.** | Returned in 1.25 s; still `['playground']`. The docstring explains why: "Derives the value from the asset's registry_id via the patch module's `SUPPORTED_SIMULATION_BACKENDS` declaration". Our registry_id is `adityas-workspace-1/skypaw-day1b-rubble`. |
| D | `PATCH /api/v1/assets/{uuid}` `supported_simulation_backends` (attempt 2) | **PASS. MuJoCo enabled, renders, has collision.** | `cw.api.src_app_api_assets_update_asset(uuid, AssetUpdateSchema(supported_simulation_backends=["playground","mujoco"]))` → `['playground','mujoco']`, `build_status: 'built'`. The twin inherits it; `visual_asset_status: renderable, render_fields ['glb_file','urdf_file']`. Preflight eligible. **R2-3 started normally with the rubble present.** It shows as three grey boxes in the Go2 camera, and **the Go2 pushed it from (2.00, 0.00) to (2.71, −0.42), yaw −4°**: the collision geometry is real, but the body is free (`fixed_base: False`). |

Time to ready: 43.7–54.0 s (mean 50.4 s over 3 runs, 49.4 s in round 1). First rendered drone frame: available at ready (not timed more finely). MQTT state starts flowing at about +23 s.

## SDK transport (check A, exact code)
- `cyberwave/mqtt/__init__.py:308-312` builds the client with no transport, and nothing in `config.py` or `mqtt/__init__.py` mentions `websockets` or a transport option. The only "transport" hits are docstrings (`mqtt/__init__.py:185`, `config.py:84`: "Enable TLS transport"):
  ```python
  self.client = mqtt.Client(
      callback_api_version=CallbackAPIVersion.VERSION2,
      client_id=self.client_id,  # type: ignore
      protocol=self._protocol,
  )
  ```
- Port and TLS are configurable: `CYBERWAVE_MQTT_PORT=443`, `CYBERWAVE_MQTT_USE_TLS=1`.
- Working scratch patch (`r2_sdk_ws.py`, imported before `Cyberwave()`): subclass `paho.mqtt.client.Client` with `kw["transport"]="websockets"`, then `self.ws_set_options(path="/")`, and assign it to `cyberwave.mqtt.mqtt.Client`. With this, the SDK's own `takeoff`, `move_forward`, `navigation` status subscription and `subscribe` all ran over WSS.
- Username: when the username is `mqttcyb` and the password is a `cw_` token, the SDK connects as `cwh_<sha256(token)>` (`config.resolve_mqtt_credentials`, `mqtt_identity.py`). The broker accepted both.

## Discrepancies with docs/RESEARCH.md (round 2)
- **D-R2-1 (§1.7, §3, D2): MQTT works over WSS 443 at path `/`**, and the Python SDK can use it with a small transport patch. "Python SDK has no WebSocket transport" is true of the configuration but not a blocker. The browser URL is `wss://mqtt.cyberwave.com/` (not `/mqtt`). Cross-twin `+` wildcards are not authorized. Per-twin `#` and per-environment `#` are.
- **D-R2-2 (§1.5, D2): the simulated pose is on MQTT, not REST.** Topics: `position`, `rotation`, `link_poses`, `free_joint_state`. For the Go2, `position` starts only when its nav stack comes up. REST has no live pose (`/state` GET 405, `/telemetry` = events, `/joint_states` = home pose). "Poll the twin pose via REST" is not possible; use MQTT/WSS.
- **D-R2-3 (new): `cw.affect()` disconnects and discards the SDK MQTT client when the mode changes** (`client.py` ≈1026–1030: `self._mqtt_client.disconnect(); self._mqtt_client = None`). Subscriptions made before `affect` are silently lost. This is why runs R2-1 and R2-2 saw zero messages. Rule: call `affect` first, then subscribe, or use a dedicated capture client.
- **D-R2-4 (§0 D1, §1.2): `skydio/skydio-x2` runs in MuJoCo** (px4vision still blocked by its depth sensor, round 1). The X2 must be placed above ground (editor z=0.5). At z=0 every camera is black. Usable sensors: `camera_chasis_bottom` (nadir) and `payload_color_camera` (forward). The front/back nav cameras render black (they probably look at the black sky). All sensors render at 640×360. There is no depth, so a depth fallback is needed (`intel/realsensed455` twin, untested).
- **D-R2-5 (§1.3): `takeoff` works in sim over MQTT.** The first call after the auto-attach of the "Python SDK" policy may be lost (the SDK warns). Send it once more after a few seconds, or call `drone.policy.ensure_attached()` first. Separately, `TwinNavigationCommandSchema` lists `takeoff`/`hover`/`land` as REST navigation commands; untested.
- **D-R2-6 (§1.5, D5): `navigation.goto`/`follow_path` do not execute in an unmapped sim scene,** with either the default teleop controller or the "Go2 Autonomous Navigation" policy. They stay `queued` and the lifecycle stays `idle`. Velocity commands (`locomotion.move_forward` → MQTT `command`, about 20 Hz burst + `stop`) do move the Go2. There is no obstacle avoidance on that path: it pushed the rubble.
- **D-R2-7 (§1.6, D6): the custom GLB path works without the editor.** Steps: `assets.create` → raw `src_app_api_assets_upload_glb(uuid, file=(name, bytes))` → `src_app_api_assets_update_asset(uuid, AssetUpdateSchema(supported_simulation_backends=["playground","mujoco"]))` → `cw.twin(asset_uuid, environment_id=)`. The collision is real. The body is dynamic; `fixed_base=True` in the same PATCH is the likely fix (untested). `sync-simulation-backends` only applies to catalog assets. The signed-URL storage host is `storage.googleapis.com`.
- **D-R2-8 (§1.3): `total_duration_s` reports the requested `duration` (120.0) even for runs stopped early.** Elapsed time is not visible from the simulation detail.

## Credits (round 2)
| Run | `affect` → stop | Notes |
|---|---|---|
| R2-1 | ≈126 s (server cap 120 s) | Go2 + X2 at z=0 |
| R2-2 | ≈89 s | X2 at z=0.5, takeoff attempt 2 |
| R2-3 | ≈71 s | + rubble, MQTT capture, move_forward |
| **Total** | **≈286 s elapsed (≈0.048 credits)**; ≤360 s / 0.06 credits if billed at the requested `duration` | Within the ≤6 min cap |

## Recommendations (round 2)
- **Drone slug:** use `skydio/skydio-x2`, spawned at z≥0.5, with `camera_chasis_bottom` for mapping/overwatch and `payload_color_camera` for forward views. Add an `intel/realsensed455` twin only if depth is required. Drop `holybro/px4vision` until Cyberwave fixes `color_mode`.
- **MQTT transport:** MQTT over `wss://mqtt.cyberwave.com:443/` from the backend, using the SDK plus a pinned transport shim. Subscribe after `affect` to per-twin `#` topics. Ask Cyberwave for a supported `CYBERWAVE_MQTT_TRANSPORT`.
- **Go2 navigation approach:** our own grid planner sending velocity commands (`locomotion.move` / `move_forward` / `turn_*`), closed-loop on MQTT `position`/`rotation` (≥8 Hz). Treat `navigation.goto` as a stretch goal behind an occupancy-map test.
- **Custom GLB path:** raw `upload_glb` with `(filename, bytes)`, then PATCH `supported_simulation_backends` to include `mujoco`, plus `fixed_base=True` for static rubble (verify). No editor "Prepare simulation" step is needed.
