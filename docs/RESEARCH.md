# RESEARCH.md — Phase 0: verified facts

**Verified on:** 2026-10-31 · **By:** Fable (governor) with two verification agents and local SDK introspection
**Sources:** PyPI (`cyberwave` 0.7.4, `sarvamai` 0.1.41) installed and read from source; docs.cyberwave.com (raw `.md` pages); `cyberwave-os/cyberwave-python` at commit 85937673; cyberwave.com catalog pages (embedded asset JSON); docs.sarvam.ai via `llms.txt` and ~48 pages; live reachability tests from this cloud environment.

Legend: **CONFIRMED** = seen in current docs or source · **CORRECTED** = SPEC.md or earlier notes were wrong, fixed here · **UNVERIFIED** = could not be confirmed, test with a real key on day one.

---

## 0. Decisions this document forces

| # | Finding | Impact on SPEC.md | Proposed action |
|---|---|---|---|
| D1 | **`dji/mini-4-pro` is not MuJoCo-compatible.** Catalog asset JSON: `supported_simulation_backends: []`. Cyberwave refuses to start a MuJoCo sim if any twin in the environment is not marked compatible. Its capabilities say `navigation_coordinate_frames: ["geodetic"]`, `navigation_requires_runtime_probe: true`: it is a live-hardware (DJI Wayline) asset. | SPEC §3.1 and all mockup labels name `dji/mini-4-pro`. Drone frames, mapping and overwatch would not run in MuJoCo. | **Replace the drone with `holybro/px4vision`** (MuJoCo-marked; RGB `front_color_camera` 100° + `front_depth_camera` 46°, 0.3–10 m; `can_fly`). Alternate: `skydio/skydio-x2` (MuJoCo-marked; 7 RGB cameras incl. `camera_chasis_bottom`, no depth). Needs your approval because it changes the spec. |
| D2 | **MQTT 8883 is blocked from this cloud environment.** TCP connect to `mqtt.cyberwave.com:8883` times out; the environment proxy passes HTTPS only. All HTTPS hosts are reachable. | Realtime twin state (position, `navigate/status`, frames topics) cannot be received here over MQTT. | Phase 1 (ADR-001) must either (a) host the backend where outbound 8883 is allowed, or (b) poll REST (`GET /api/v1/twins/{uuid}/latest-frame`, `GET /api/v1/events/live`, twin GET) at 2–5 Hz. Decision in ADR-001; no spec change. |
| D3 | **No API keys are set** in this environment (`CYBERWAVE_API_KEY`, `SARVAM_API_KEY` absent). | Nothing live can run before M1. | Add both as environment secrets before M1 starts. |
| D4 | **Cyberwave free credits are UNVERIFIED.** Billing docs mention card top-ups and coupon codes only; the earlier "50 credits/month" claim could not be re-confirmed. MuJoCo costs **0.01 credits/min** (SDK constant). | Budget risk only. | Check the credit balance in the Cyberwave dashboard before M1; redeem any hackathon coupon. |
| D5 | **Obstacle-aware Go2 navigation in sim requires a mapping run or a generated occupancy map** (Go2 has `mapping_service: cyberwave_slam · RTAB-Map`, `navigation_obstacle_avoidance: true`). Behaviour of `goto` without a saved map is undocumented. | SPEC §12 already plans our own grid planner with dense `follow_path` waypoints. | Keep our planner (SPEC §6.4). Optionally also run Cyberwave's "Generate from scene" occupancy map for the Go2 as a stretch. No spec change. |
| D6 | **Collision geometry for a GLB-only asset is UNVERIFIED**; MuJoCo readiness depends on the UI-only "Prepare simulation" flow, which "may use credits". Collision normally comes from URDF STL meshes. | M1 risk. | M1 task includes: upload GLB → run Prepare simulation in the editor → if blocked, package the rubble as a URDF ZIP with STL collision meshes (512 MB limit). Decision point inside M1, not a spec change. |

Everything else in SPEC.md is feasible as written.

---

## 1. Cyberwave

### 1.1 SDK and client
| Item | Status | Fact |
|---|---|---|
| Package | CONFIRMED | `cyberwave` **0.7.4** (PyPI, 2026-10-26), `requires_python >=3.10,<4.0`; quickstart examples say 3.11+. Deps: cyberwave-contracts, cyberwave-geometry, numpy, paho-mqtt, pydantic, pyyaml, urllib3. Camera module imports `aiortc` (install `cyberwave[camera]` only if streaming video is needed; `get_frame` via REST does not need it). |
| Client | CONFIRMED | `from cyberwave import Cyberwave; cw = Cyberwave()` reads `CYBERWAVE_API_KEY`. Constructor: `Cyberwave(base_url=None, api_key=None, token=None, mqtt_host=None, mqtt_port=None, environment_id=..., workspace_id=...)`. |
| Env vars | CONFIRMED | `CYBERWAVE_API_KEY`, `CYBERWAVE_ENVIRONMENT_ID` (else a "Quickstart Environment" is auto-created), `CYBERWAVE_WORKSPACE_ID` (multi-workspace tokens; else `400 workspace_required`), `CYBERWAVE_BASE_URL` (default `https://api.cyberwave.com`, SDK source only), `CYBERWAVE_MQTT_HOST/PORT/USERNAME(mqttcyb)/PASSWORD/USE_TLS/TLS_CA_CERT/TOPIC_PREFIX`. |
| REST | CONFIRMED | Base `https://api.cyberwave.com`, `Authorization: Bearer <key>` (`Token` also accepted), paths `/api/v1/...`. Full reference: docs.cyberwave.com/api-reference/rest/DefaultApi.md. |
| MCP | CONFIRMED | `https://mcp.cyberwave.com/mcp` (Streamable HTTP, Bearer key); tools include `cw_create_environment`, `cw_create_waypoint`, `cw_navigation_goto`, `cw_start_simulation`, `cw_capture_frame`, `cw_prepare_simulation_asset`. |

### 1.2 Catalog slugs (asset JSON from cyberwave.com/<vendor>/<slug>)
| Slug | MuJoCo | Sensors / notes |
|---|---|---|
| `unitree/go2` | **yes** (`playground, mujoco`) | `front_camera` rgb 1280×720 88.5°; `lidar_4d`; `can_locomote`, legged, `navigation_obstacle_avoidance: true`, mapping service Cyberwave SLAM. RL locomotion policy in sim (changelog 2026-07-28). |
| `holybro/px4vision` | **yes** | `front_color_camera` rgb 4056×3040 100°; `front_depth_camera` depth 46°, 0.3–10 m; `can_fly`, battery. **Recommended drone.** |
| `skydio/skydio-x2` | **yes** | 7 rgb cameras 1280×720 (front L/R, back L/R, chassis top/bottom) + `payload_color_camera` 3840×2160; no depth; `can_fly`, obstacle avoidance flag true. Alternate drone. |
| `dji/mini-4-pro` | **no** (`[]`) | rgb `main_camera` + `gnss`; geodetic navigation, runtime probe required: live-hardware asset. Do not use in sim. |
| `dji/mavic-3-enterprise-2`, `parrot/anafi-ai` | playground only | — |
| `dji/mini-3` (none), `dji/tello` (playground) | no | — |
| `cyberwave/standard-cam` | yes | `color_camera` rgb 1280×720 58.7°; stationary. Use for fixed site cameras. |
| `intel/realsensed455` | yes | depth-capable camera twin (for depth frames if the drone's depth sensor does not render). |
| `cyberwave/rgb-camera-with-depth-estimation` | playground only | — |
| Payload | — | `generic-drone-payloads/uwb-radar-pod` exists as a catalog item (visual only; our radar stays synthetic). |

### 1.3 Simulation control
| Item | Status | Fact |
|---|---|---|
| `cw.affect("simulation")` | CONFIRMED | Aliases `sim`, `mujoco`. **Auto-starts a billable MuJoCo instance** for the twin's environment; `duration=` seconds; create twins before calling. `playground` = free kinematic, no frames. `live`/`real-world` = hardware (forbidden in this project). |
| Explicit control | CONFIRMED | `cw.environments.simulations.start(env_id, backend="mujoco", stream_data=True, duration=...)` → `Simulation` with `.wait_until_active()`, `.stop()`; `.get_active(env_id)`, `.list(env_id)`. REST `POST /api/v1/environments/{uuid}/simulations`, `POST .../simulations/{id}/stop`. One sim per environment (409 otherwise). `cw.disconnect()` does **not** stop the sim. |
| Compatibility gate | CONFIRMED | Sim start checks every twin; any twin not marked for the backend blocks start. Per-twin override exists in the editor (effect UNVERIFIED). |
| Cost | CONFIRMED | `SIMULATION_CREDITS_PER_MINUTE = 0.01` (0.6/h). Credit price and free allocation UNVERIFIED. |
| Flight/locomotion in sim | CONFIRMED | `takeoff`, `land`, `hover`, `ascend`, `descend`, `gimbal_rotate` are decorated `@simulation_level(SimLevel.PLAYGROUND)` → allowed in sim. The `affect()` docstring claiming they raise `NotSimulatedError` in sim is stale. Gimbal behaviour in MuJoCo UNVERIFIED (px4vision has no gimbal; point the camera by flying/yawing). |

### 1.4 Frames
| Item | Status | Fact |
|---|---|---|
| `camera.get_frame(format)` | CONFIRMED | `format` ∈ `bytes` (JPEG, default), `numpy` (BGR; needs numpy + opencv), `pil`, `path`. In simulation runtime mode **only `source="cloud"`** is allowed → REST `GET /api/v1/twins/{uuid}/latest-frame` (sim vs tele chosen from `affect`). Requires a running physics sim (`SimLevel.PHYSICS`) else `SimulationNotRunningError` / `SimulationLevelError`. `mock=True` returns a deterministic frame (useful for tests without credits). `sensor_id=` selects the camera; `get_frames(n, interval_ms=)`. |
| Rate | CONFIRMED | ~5 fps cap on `latest-frame` (docs); plan perception at ≤5 Hz. |
| Depth | CONFIRMED | MuJoCo only: `twin.camera["<depth_sensor>"].get_frame()` → float32 metres (`raw=True` for uint16 mm). Whether px4vision's `front_depth_camera` renders in MuJoCo is UNVERIFIED; fallback is an `intel/realsensed455` twin. |
| Streaming video | CONFIRMED | `get_video()` / WebRTC needs TURN hosts (see §3); not required for `get_frame`. |

### 1.5 Navigation and waypoints
| Item | Status | Fact |
|---|---|---|
| Handle | CONFIRMED | `twin.navigation` → `TwinNavigationHandle` (`cyberwave/motion.py`): `goto(position=[x,y,z], *, rotation, yaw, reference_frame, constraints, metadata, actions, planner, ...)` → dict with `action_id`; `follow_path(waypoints | NavigationPlan, *, wait_s, max_loops, yaw)`; `relative_move([dx,dy,dz], frame="body")`; `stop()`; `wait_for_completion(action_id, timeout=120, raise_on_failure=True)` subscribes to MQTT `cyberwave/twin/{uuid}/navigate/status` (terminal: completed/failed/cancelled/blocked). REST `POST /api/v1/twins/{uuid}/navigation`. `planner` kwarg is for arms (LIN/PTP), not mobile robots. |
| Builder | CONFIRMED | `from cyberwave.navigation import NavigationPlan; NavigationPlan().waypoint(x, y, z, yaw=, duration_seconds=, actions=)`. |
| Environment waypoints | CONFIRMED | `cw.environments.create_waypoint(env_id, *, position={x,y,z}, name, rotation, waypoint_id, collection, metadata)`, `create_waypoints(env_id, [...])`, `get_waypoints`, `update_waypoint`, `delete_waypoint`. REST `/api/v1/environments/{uuid}/waypoints`. Metadata is free-form: we store `{victim_id, fused, life_sign, suggested_triage, sensors}`. |
| Obstacle avoidance in sim | PARTIAL | Documented for Go2 and MiR250 after a mapping run / generated occupancy map ("Aerial robots are not supported" for generated maps). Without a map: UNVERIFIED (assume straight line). → we plan on our grid and send dense waypoints. |
| `wait_for_completion` without MQTT | CORRECTED | It needs MQTT. Where 8883 is blocked, poll the twin pose via REST / `latest-frame` and detect arrival ourselves. |

### 1.6 Scene and assets
| Item | Status | Fact |
|---|---|---|
| Environment | CONFIRMED | `cw.environments.create(name, project_id=None, description="")`; twins: `cw.twin("unitree/go2", environment_id=env.uuid)`; editor pose: `twin.edit_position(x,y,z)`, `edit_rotation(yaw=)`, `edit_scale()`. |
| GLB upload | CONFIRMED | `asset = cw.assets.create(name=, description=)`; `cw.assets.upload_glb(asset.uuid, path)`: direct multipart ≤32 MB (`POST /api/v1/assets/{uuid}/glb-file`), signed-URL flow above. Place: `cw.twin(asset.uuid, environment_id=...)`. |
| Simulation readiness | CONFIRMED (UI only) | Editor → *Simulation readiness → Prepare simulation* (checks, proposed changes, "may use credits"). No SDK/REST entry documented; MCP tool `cw_prepare_simulation_asset` exists. Collision from GLB alone: UNVERIFIED (see D6). URDF ZIP with STL collision: `POST /api/v1/assets/create-with-urdf`, 512 MB. |
| Occupancy map | CONFIRMED | UI "Maps → Generate from scene" (ground robots only); `POST /api/v1/twins/{uuid}/occupancy-map`; `GET /api/v1/maps/{uuid}/point-cloud.ply`. |
| Preview | CONFIRMED | `cw.environments.create_preview(env_uuid)`. |

### 1.7 Alerts, events, workflows, push
| Item | Status | Fact |
|---|---|---|
| Alerts | CONFIRMED | REST `POST/GET /api/v1/alerts`, `.../{uuid}/acknowledge|resolve|silence`; schema: name, description, severity (`info|warning|error|critical`), alert_type, twin_uuid/environment_uuid/workflow_uuid (≥1 required), metadata, force. SDK `twin.alerts.create(...)`, `.list(status="active")`, `.acknowledge()`, `.resolve()`. Dedupe unless `force=True`. |
| Events | CONFIRMED | `POST /api/v1/events` (environment_uuid, event_type required; twin_uuid, severity, data, tags, correlation_id); `GET /api/v1/events/live` (JSON list, cursor `last_timestamp_ms`, `limit` default 100: **polling**, not SSE). SDK `cw.publish_event(twin_uuid, event_type, data)` publishes via MQTT only. |
| Workflows | CONFIRMED | Node `twin_control` ("Move Twin": follow waypoints / reach area / patrol / inspect area; flight transitions for aerial twins), `send_alert`, `http_request`, `send_email`, `send_mqtt`, `conditional`, `call_model`. Triggers: manual, mission, schedule, webhook (inbound; URL/auth "not documented yet"), mqtt, event, alert, camera_frame. |
| Outbound push | CORRECTED | **No outbound webhooks.** Options: MQTT subscriptions; workflow `http_request` node; polling `events/live`, `alerts`, `latest-frame`, twin GET. |
| MQTT topics | CONFIRMED | `cyberwave/twin/{uuid}/position|rotation|telemetry|navigate/status|navigate/command|camera/photo|depth|pointcloud|event|command`, `cyberwave/environment/{uuid}/{update_type}`, `cyberwave/workflow-run/{run_uuid}/status`. Broker `mqtt.cyberwave.com:8883` TLS, username `mqttcyb`, password = API key. Browser: `wss://mqtt.cyberwave.com` (443). Python SDK has no WebSocket transport. |

---

## 2. Sarvam AI

### 2.1 Common
| Item | Status | Fact |
|---|---|---|
| Base / auth | CONFIRMED | `https://api.sarvam.ai`; header `api-subscription-key: <key>` (or `Authorization: Bearer`). Bad key → **403** `invalid_api_key_error`; out of credits → 402 `insufficient_quota_error`; 429 codes `rate_limit_exceeded_error`, `model_rate_limit_exceeded_error`, `seat_budget_exhausted_error`, `audio_duration_busy_error`. |
| SDKs | CONFIRMED | Python `sarvamai` **0.1.41** (`SarvamAI`, `AsyncSarvamAI`, reads `SARVAM_API_KEY`); JS `sarvamai` 1.1.14 (`SarvamAIClient`). Keys at `indus.sarvam.ai/key-management` (browser only). |
| Language codes | CONFIRMED | BCP-47 `xx-IN`. **Odia is `od-IN` everywhere except the realtime STT socket, where it is `or-IN`.** |

### 2.2 Speech to text
| Item | Status | Fact |
|---|---|---|
| Realtime WS | CONFIRMED | `wss://api.sarvam.ai/speech-to-text-realtime/ws?language_code=auto&model=saaras:v3-realtime&stream_type=fast|balanced|simulated&mode=codemix&endpointing=vad|manual&encoding=linear16&sample_rate=16000[&keyterms=<json array, v4 only>]`. `language_code` is **required** (`auto` allowed). Browser auth via subprotocol `api-subscription-key.<key>`; server auth via header. Client events: `audio_input{audio: base64}`, `speech_start`, `speech_end`, `flush`, `config.update`, **`end`** (not `session.end`), `ping`. Server: `session.begin`, `vad.speech_start/end`, `transcript.partial`, `transcript.final` (text, language, confidence, timing), `config.updated`, `pong`, `session.end` (billed `audio_duration_s`), `error`. Close codes 1003 (quota/key), 1008 (inactivity: send `ping`), 1011, 4000 (bad param / **account not enabled**). Send ~3200-byte chunks every 100 ms. Mode applies to finals only. **Beta/enablement status UNVERIFIED**: test the 101 handshake with the real key on day one. SDK: `AsyncSarvamAI().speech_to_text_realtime_streaming.connect(language_code=, model=, stream_type=, mode=, encoding=, sample_rate=, keyterms=)`. |
| Legacy WS | CONFIRMED | `wss://api.sarvam.ai/speech-to-text/ws` (model default now `saaras:v4`, no partials, `language-code=unknown`, Odia `od-IN`). Fallback only. |
| REST | CONFIRMED | `POST /speech-to-text` multipart `file`; `model` `saaras:v4` (default) / `saaras:v3`; `mode` transcribe/translate/verbatim/translit/**codemix** (works on v4 despite stale ref text); ≤30 s per request (422 above); 16 kHz best; PCM needs `input_audio_codec`; `keyterms` JSON array ≤50×64 chars (v4 only); response `{request_id, transcript, language_code, timestamps?, language_probability?}`. 23 languages. SDK `client.speech_to_text.transcribe(file=, model=, mode=, language_code=, keyterms=)`. |
| Price / limits | CONFIRMED | ₹0.49/min billed per second (PAYG). REST 60 rpm; WS 20 concurrent. |

### 2.3 Chat completions (mission plans)
| Item | Status | Fact |
|---|---|---|
| Endpoint / models | CONFIRMED | `POST /v1/chat/completions`; **only** `sarvam-105b` (128K) and `sarvam-105b-conversations` (32K). `sarvam-30b` is **deprecated** ("migrate to Sarvam-105B") and absent from the SDK enum. |
| Structured output | CONFIRMED | `response_format={"type":"json_schema","json_schema":{"name","schema","strict":true}}`; JSON arrives as a string in `message.content`. Also `json_object`, `text`. `tools` + `tool_choice` (`none|auto|required|{function}`) supported. |
| `reasoning_effort` | **VERIFIED LIVE** | Server accepts only `low`, `medium`, `high` (or omit). `none`/`max` → 400 "Input should be low, medium or high". SDK 0.1.41 literals are wrong (`none|low|high|max`): pass the value as a plain string. Default is reasoning ON; reasoning tokens bill as output and count toward `max_tokens` (set ≥1024 for plans). SDK accepts `none|low|high|max` literals; pass `null`/omit if `none` is rejected. |
| Limits / price | CONFIRMED | `max_tokens` Starter 4096 / Pro 16384 / Business 128000 (default 2048). ₹15/M input, ₹5/M cached, ₹60/M output. 60 rpm PAYG. `stream: true` → SSE with `reasoning_content` deltas. |
| SDK | CONFIRMED | `client.chat.completions(messages=, model="sarvam-105b", reasoning_effort=, max_tokens=, response_format=, tools=, tool_choice=, stream=)`. |

### 2.4 Text to speech (Go2 speaker)
| Item | Status | Fact |
|---|---|---|
| REST | CONFIRMED | `POST /text-to-speech` JSON → `{request_id, audios: [base64 WAV...]}`; `POST /text-to-speech/stream` raw audio. Fields: `text` (≤2500 chars v3), **`language_code`** (SDK 0.1.41 also uses `language_code`; only the WS Python helper uses `target_language_code`), `model` `bulbul:v3` (REST default) / `bulbul:v4-flash` / `bulbul:v2`, `speaker` lowercase, `pace` 0.5–2.0, `speech_sample_rate` 8000…48000 (default 22050), `output_audio_codec` wav|mp3|linear16|mulaw|alaw|flac|aac (opus errors on v3), `temperature`, `dict_id`, `enable_cached_responses`. |
| Languages / voices | CONFIRMED | v3: hi, bn, ta, te, gu, kn, ml, mr, pa, od, en (-IN). v3 speakers include `shubh` (default), `priya`, `rohan`, `kavitha` (our picks) among 37. v4-flash: persona IDs `<name>_<lang>_<style>`, no ml/od, ₹1/1k chars vs ₹3 for v3. |
| WS TTS | CONFIRMED | `wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true` (**default model is v2: always pass model**); messages config/text/flush/ping → audio chunks base64. Not needed in v1 (REST + cache). |
| Limits | CONFIRMED | REST 60 rpm, WS 60 concurrent (PAYG). |

### 2.5 Translation (comms, log, phones)
| Item | Status | Fact |
|---|---|---|
| REST | CONFIRMED | `POST /translate` `{input (≤2000 chars for sarvam-translate:v1, ≤1000 mayura), source_language_code (required; "auto" only with mayura:v1), target_language_code, model: "sarvam-translate:v1" | "mayura:v1", mode (formal only for sarvam-translate), speaker_gender, numerals_format, output_script (mayura)}` → `{request_id, translated_text, source_language_code}`. Default model when omitted: UNVERIFIED (always pass it). 23 languages. ₹2/1k chars, 60 rpm. SDK `client.text.translate(input=, source_language_code=, target_language_code=, model=)`. |

### 2.6 Credits
DOCS say ₹100 free credits per verified signup. **LIVE CHECK 2026-10-31: this key has ₹0; every billable call → 402 `insufficient_quota_error`.** Owner must claim/add credits at indus.sarvam.ai before M5. Budget math for the hackathon: ₹100 ≈ 200 min STT, or ~33k TTS chars (v3), or ~50k translated chars. Hence: cache TTS and translations, translate only on change, keep the realtime socket open only while the mic is held.

---

## 3. Network: hosts the backend must reach

Tested from this cloud environment on 2026-10-31. **HTTPS passes; raw TCP does not.**

| Host | Port / protocol | Purpose | Reachable here | Needed by |
|---|---|---|---|---|
| `api.cyberwave.com` | 443 HTTPS | REST, SDK, `latest-frame`, waypoints, alerts, events, sim control | **yes** | backend |
| `mqtt.cyberwave.com` | **8883 MQTT/TLS** | twin position/rotation, `navigate/status`, frames/depth topics, `wait_for_completion` | **NO (blocked)** | backend (optional if polling) |
| `mqtt.cyberwave.com` | 443 `wss://` | MQTT over WebSocket for browsers | untested | browser, or backend with a WS MQTT client (SDK lacks it) |
| `cyberwave.com` / `www.cyberwave.com` | 443 | web app, API keys at `/profile`, catalog | yes | humans |
| `mcp.cyberwave.com` | 443 | MCP server (`/mcp`) | yes (404 at root is expected) | optional |
| `tls.turn.cyberwave.com` 443, `turn.cyberwave.com` UDP 3478, `stun.l.google.com` UDP 19302 | TURN/STUN | WebRTC video streams only (`get_video`) | untested | not needed for `get_frame` |
| `static.cyberwave.com` | 443 | edge-only assets | yes | not needed |
| Signed-URL storage for GLB >32 MB and downloads | 443 | asset upload/download | UNVERIFIED host (GCS-like; allow `*.googleapis.com` provisionally) | backend (M1 upload) |
| `api.sarvam.ai` | 443 HTTPS + WSS | all Sarvam REST and WebSocket endpoints (single host) | **yes** | backend |
| `indus.sarvam.ai` | 443 | dashboard, keys, billing | — | humans |
| `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` | 443 | installs | yes | build |
| `cdnjs.cloudflare.com` | 443 | Three.js for the UI (or bundle locally) | yes | browser |

**Add to the environment's allowed hosts (or choose a host that allows them):** `mqtt.cyberwave.com:8883` (raw TLS), `tls.turn.cyberwave.com:443` + `turn.cyberwave.com:3478/udp` if video streaming is ever used, and the storage host revealed by the first large GLB upload.

---

## 4. Corrections to SPEC.md (pending your approval, see §0)
1. §3.1 and §2 actors: drone slug `dji/mini-4-pro` → **`holybro/px4vision`** (or `skydio/skydio-x2`). Mockup labels and `SP.MODELS.drone` follow.
2. §3.1 STT realtime: Odia code on the socket is `or-IN`; close with `{"event":"end"}`.
3. §3.1 LLM: `reasoning_effort` → `low` (or `null` to disable), not `none`.
4. §3.3 / §12: `wait_for_completion` depends on MQTT; where MQTT is blocked, arrival is detected by polling twin pose. No behavioural change for the lead.
5. §9: Cyberwave free credits are not documented; replace "50 credits/month" with "check balance; MuJoCo 0.01 credits/min".

## 5. Day-one live checks (first hour of M1, with real keys)
1. `cw.twin("holybro/px4vision")` + `cw.twin("unitree/go2")` in one environment → `cw.affect("simulation", duration=120)` starts MuJoCo without a compatibility error.
2. `drone.camera.get_frame("numpy")` returns a rendered frame (not `None`); try `sensor_id="front_depth_camera"` for depth.
3. `drone.takeoff(altitude=6)`; `go2.navigation.goto([2,0,0])` moves the twin; observe whether it stops at an obstacle or clips through.
4. Upload a small rubble GLB; run *Prepare simulation* in the editor; note whether collision is generated.
5. Sarvam: realtime WS handshake returns 101 with the key (else fall back to REST `saaras:v4`); one `sarvam-105b` structured call; one `bulbul:v3` call per language; one translate call. Record rupees spent.

---

## 6. Day-one live results (2026-10-31) — corrections that supersede sections 1 and 3

Evidence: `docs/evidence/day1/cyberwave.md` (rounds 1–2, ≈0.07 credits), `docs/evidence/day1/sarvam.md`.

| Topic | Verified fact | Consequence |
|---|---|---|
| Drone | **`holybro/px4vision` crashes the MuJoCo worker** (`Missing required parameter 'color_mode' in camera sensor 'front_depth_camera'`) although marked compatible and passing preflight. **`skydio/skydio-x2` works**: MuJoCo starts, `takeoff(altitude=6)` flies it, frames render from `camera_chasis_bottom` (down) and `payload_color_camera` (forward), 640×360. Must spawn at editor **z ≥ 0.5 m** (at z=0 all cameras are black). | SPEC v1.2 drone = `skydio/skydio-x2`. |
| MQTT transport | **MQTT over WebSocket works: `wss://mqtt.cyberwave.com:443` path `/`** (not `/mqtt`, `/ws`). Username `mqttcyb` or the SDK's `cwh_<sha256(token)>`, password = API key. SDK 0.7.4 has no transport option; a small subclass of paho `Client(transport="websockets")` with `ws_set_options(path="/")`, `CYBERWAVE_MQTT_PORT=443`, `CYBERWAVE_MQTT_USE_TLS=1` connects the SDK. Per-twin `cyberwave/twin/{uuid}/#` and `cyberwave/environment/{uuid}/#` subscriptions are granted; cross-twin `+` wildcards are refused. | Backend uses MQTT-over-WSS everywhere (dev and Codespaces); the REST-polling fallback is no longer needed for pose. D2 resolved. |
| `affect()` side effect | `cw.affect()` **disconnects and discards the SDK MQTT client** when the mode changes; subscriptions made before it are lost silently. | Always call `affect` first, then subscribe. |
| Readiness | `running` ≠ usable. `simulation_ready` turns true at **~45–55 s**; first real Go2 frame ~24 s; Go2 `position` topic starts ~51 s at ~8 Hz; `link_poses`/`rotation` ~19 Hz from ~23 s. `affect()` can race and raise "did not become active" ~1 s after start (retry once). | Backend waits on `simulation_ready` from the simulations list, not `wait_until_active()`. |
| Pose | **No REST source of the simulated pose** (`twins.get_raw` = editor pose; `/state` 405; `/telemetry` = recording events; `/live-position` GPS-only). Pose comes only from MQTT `position`/`rotation`/`link_poses`. | Pose = MQTT. |
| Go2 navigation | `navigation.goto` and `follow_path` are accepted (`queued`, "received by the navigation bridge") but **never execute in an unmapped scene**, with either the default "Quadruped Controller" or "Go2 Autonomous Navigation". **`locomotion.move_forward(0.5, duration=4)` moves the robot** (0→2.0 m, confirmed by MQTT position). There is no obstacle avoidance: it pushed the rubble. | Our grid planner drives the Go2 with velocity commands closed-loop on MQTT pose (SPEC §6.4 already assumes our planner). `goto` with a generated occupancy map stays a stretch goal. |
| Drone flight | `takeoff` is MQTT-only (works over WSS). REST `takeoff/hover/land` in `TwinNavigationCommandSchema` untested. | Fine with WSS. |
| Custom GLB | SDK `assets.upload_glb` is broken in 0.7.4 (pydantic error). **Raw call works:** `cw.api.src_app_api_assets_upload_glb(asset_uuid, file=("rubble.glb", data_bytes))` (stored on `storage.googleapis.com`). New assets default to `['playground']` and **block MuJoCo start**; `sync-simulation-backends` does nothing for user assets; **`cw.api.src_app_api_assets_update_asset(uuid, AssetUpdateSchema(supported_simulation_backends=["playground","mujoco"]))` enables it** (no editor step). Collision is real (Go2 pushed it) but the body is free; `fixed_base=True` via the same PATCH is untested. | D6 resolved; M1 sets `fixed_base`. |
| Environments | `environments.create(name)` without `project_id` crashes on an account with no projects; pass `project_id` (project "skypaw" exists). `cw.twin(slug, environment_id=)` reuses an existing twin of the same asset. | M1 script passes project_id and looks up by name. |
| Credits | Three MuJoCo runs ≈ 286 s wall ≈ 0.05 credits; `total_duration_s` reports the requested 120 s even when stopped early, so budget on requested duration. Balance 50. | QA floor 15 stands. |
| Network hosts (add) | `storage.googleapis.com` (GLB storage, confirmed). `mqtt.cyberwave.com:443` WSS (confirmed). | Both pass the HTTPS-only proxy. |
| Sarvam | Key authenticates; `reasoning_effort` ∈ {low, medium, high} only (none/max → 400); SDK TTS takes `language_code`; realtime socket handshakes (101) and sends an `error` before `session.begin` when credits are exhausted, then closes 1003. **This session's key has ₹0**; the owner rotated the key to another account, but a running session cannot see the new value: run `scripts/checks/sarvam` in a fresh session or the Codespace. | M5 live tests happen where the new key exists. |

**M1 additions (live, 2026-10-31):** `cw.assets.list()` does not return user-created assets in 0.7.4; find them via the twin's `asset_uuid` or `cw.assets.search(name)`. `cw.assets.delete(uuid)` works. `cw.twins.create(asset_id, environment_id, name=, position_*=, rotation_*=, fixed_base=True)` creates a named twin with its pose in one call. The asset update is sent as PUT `/api/v1/assets/{uuid}`; `fixed_base=True` persists and keeps the rubble static. GLB vertices are read as Z-up world coordinates (site frame → world: x, −z, y). Repeat `make scene`: 13 GETs, 0 writes.

**Sarvam live results via GitHub Actions (new key, 2026-10-31 09:38 UTC, ≈₹0.3):** translate en→mr 1516 ms, en→ta 2231 ms (`sarvam-translate:v1`, correct output); REST STT `saaras:v4` codemix 1332 ms on a 2 s tone (returned "V-01." from keyterms; fine); TTS `bulbul:v3` 1.5–2.0 s per phrase, 22050 Hz WAV, 1.2–1.9 s audio; SDK `text_to_speech.convert(language_code=...)` OK 1812 ms; realtime socket: 101 in 734 ms, `session.begin` at 754 ms, `session.end` after `end` with `audio_duration_s` 1.0 billed; `reasoning_effort="bogus"` → 400 "low, medium or high". **Plan LLM problem:** `sarvam-105b` with `reasoning_effort=low`, `max_tokens=512`, strict json_schema returned `finish_reason=length`, `content=null`, `reasoning_tokens=511` in 5545 ms: reasoning consumes the budget and no JSON arrives. **Decision: plan with `sarvam-105b-conversations`** (no reasoning, 32K ctx, same price) and keep `sarvam-105b` only as a fallback with `max_tokens ≥ 2048`. Realtime `silence_duration_ms` default on the server is 1000 (docs said 500).

**Voice pipeline live (Actions run 4, 09:49 UTC, ₹0.20):** plan via `sarvam-105b-conversations`, no `reasoning_effort`, strict json_schema accepted → `finish_reason=stop`, 45 completion tokens, **404 ms**, `{"intent":"navigate_go2","target":"V-01","language":"hi-IN","confidence":0.95}`; TTS hi-IN `priya` 1636 ms (TTFB 913 ms, 2.9 s audio); REST STT `saaras:v4` codemix 1099 ms → "Go2 Ko V-01 KePaas Bhejo." (tagged en-IN); realtime `saaras:v3-realtime` partial at 2250 ms after first chunk, final 350 ms after `end`, text "डॉग को V01 के पास भेजो". Per-call cost: translate ₹0.068, plan ₹0.016, TTS ₹0.069, STT REST ₹0.024, realtime ₹0.028. The workflow `voice-check.yml` reproduces this on demand.

**Scene geometry (stream E, verified from `cw.environments.export_mujoco_scene(env_id, timeout=180)` → ZIP with `mujoco_scene.xml`, `assets/*.obj`):** Cyberwave converts a whole GLB asset (all nodes) into ONE OBJ mesh and one `type="mesh"` collision geom at the twin pose, vertices used unchanged as world metres (no recentring). MuJoCo collides a mesh as its **convex hull**, so a multi-box rubble GLB becomes one solid blob covering the footprint; the Go2 stalled on that hull 8 m from V-01 while the visuals looked right (IoU 0.94 vs site.yaml from the drone frame). **Fix:** one asset + fixed twin per convex box (`ensure_rubble_pieces()`, 18 twins `skypaw-rubble-00..17`); re-export shows 18 geoms of 8 vertices. The legacy merged twin is deleted by `build_scene`. Marker colours are dropped on import (everything renders grey): detection uses YOLO first, then a shape/size marker fallback. `cw.twins.delete(uuid)` works.

**SDK bug (stream A, live):** in cyberwave 0.7.4 `locomotion.turn_right(rate, duration)` publishes `angular_z=+abs(rate)`, identical to `turn_left`, so both turn counter-clockwise in MuJoCo (heading error grows after each "right" turn). Use `locomotion.move(linear_x=0.0, angular_z=±rate, duration=d)` with a signed rate for turning (sign verified live by stream E's follow-up). Drone survey: `move_forward(2.2, duration=1.0)` travels 1.9 m on average, up to 2.8 m (momentum); bursts of 0.85 s keep frame spacing ≤ 2.5 m. Ready in 44–53 s; 78 frames over 10 corners; MQTT 34,730 pose messages in a 300 s run.
