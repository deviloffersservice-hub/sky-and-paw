# PROTOCOL.md — v1 contract between backend, console and workers

All five v1 work streams build against this file. Change it only through Fable.

## Coordinates and units
- **World frame = Cyberwave world frame** as delivered by MQTT `position`: `[x, y, z]` metres, **z up**, y is the ground axis perpendicular to x. The rubble GLB and `site.yaml` are authored in a *site frame* (x, z ground, y up); `skypaw.scene.site.site_to_world()` converts (x, −z, y). Everything on the wire is world frame.
- Angles in radians, yaw about +z, counter-clockwise from +x.
- Time: `t` is seconds since mission start (float); `ts` is ISO-8601 UTC.

## HTTP (FastAPI, port 7860)
| Route | Purpose |
|---|---|
| `GET /healthz`, `GET /version` | as T0 |
| `GET /api/state` | the same JSON as the WebSocket `snapshot` (for tests and curl) |
| `POST /api/command` | same body as a WebSocket client message; returns `{accepted: bool, reason?}` |
| `GET /frames/{frame_id}.jpg` | captured camera frame (640×360 JPEG) |
| `GET /tts/{sha}.wav` | cached Bulbul audio |
| `GET /evidence/...` | static evidence files for the demo strip |
| `WS /ws` | live channel (below) |

Auth v1: a shared token per role. Client sends it in the `hello` message, never in the URL. Tokens come from `SKYPAW_LEAD_TOKEN` / `SKYPAW_TEAM_TOKEN`; if unset (dev), any hello is accepted. The server checks the `Origin` header against the CORS allowlist and closes with 1008 otherwise.

## WebSocket `/ws`
Text frames are JSON objects with a `type`. **Binary frames are microphone audio**: int16 little-endian PCM, 16 kHz, mono, ~100 ms per frame (3200 bytes), sent only between `mic start` and `mic stop`.

### Client → server
```json
{"type":"hello","role":"lead|team","member":"t2","token":"..."}
{"type":"mic","state":"start|stop","lang":"auto|hi-IN|mr-IN|ta-IN|en-IN"}
{"type":"text_command","text":"dog को V-01 के पास भेजो","lang":"hi-IN"}      // typed fallback: same path as a final transcript
{"type":"confirm","plan_id":"P-3"}   {"type":"cancel","plan_id":"P-3"}
{"type":"decide","decision_id":"D-7","action":"approve"}                      // generic decision action
{"type":"command","intent":"aerial_survey|navigate_go2|speak|stop_all|land","target":"V-01","params":{}}   // UI buttons; produces a plan card like voice does
{"type":"triage","victim_id":"V-01","category":"immediate|delayed|unknown|expectant"}
{"type":"dismiss","victim_id":"D-01"}
{"type":"tts","phrase_id":"p_help","lang":"mr-IN"}   or   {"type":"tts","text":"...","lang":"mr-IN"}
{"type":"ping"}
```

### Server → client
`snapshot` every 200 ms (5 Hz) to every client, plus `event` messages as they happen.
```json
{"type":"snapshot","t":123.4,"ts":"...",
 "mission":{"id":"SP-0417","phase":"idle|mapping|search|reach|contact|done","since_collapse_s":2520},
 "sim":{"status":"stopped|starting|running|ready|error","ready":true,"env_id":"...","requested_s":300,"elapsed_s":87.2,"credits_est":0.05,"error":null},
 "robots":{
   "drone":{"pos":[x,y,z],"yaw":0.0,"mode":"landed|takeoff|survey|hold|flying|landing","battery":84,"pose_age_s":0.1,"frame_id":"f_000123"},
   "go2":{"pos":[x,y,z],"yaw":0.0,"mode":"idle|moving|arrived|blocked","battery":71,"pose_age_s":0.1,"target":"V-01","frame_id":"f_000124","path":[[x,y,z],...]}
 },
 "survey":{"pct_mapped":64,"frames":41,"waypoint_idx":6,"waypoints":[[x,y,z],...]},
 "grid":{"x0":-13,"y0":-9,"cs":0.5,"w":52,"h":36,"cls":"<base64 of w*h bytes: 0 unmapped,1 passable,2 difficult,3 blocked>","seen":"<base64 bitmask>"},
 "victims":[{"id":"V-01","pos":[x,y,z],"status":"located|enroute|arrived|contact|extracted|dismissed","fused":0.93,"life_sign":true,
             "evidence":{"rgb":0.91,"thermal":5.8,"radar":{"bpm":64,"snr":"strong"},"acoustic":null,"co2":null},
             "suggested_triage":"immediate","triage":null,"frames":["f_000040"],"source":{"rgb":"real","thermal":"synthetic","radar":"synthetic"}}],
 "decisions":[{"id":"D-7","kind":"plan|speak|alert|warn","title":"...","body":"...","created_t":120.1,
               "plan":{"id":"P-3","intent":"navigate_go2","target":"V-01","steps":["..."],"risk":"low","est_s":48,"source_text":"...","source_lang":"hi-IN","model":"sarvam-105b"},
               "actions":[{"id":"go","label":"Confirm · Go","primary":true},{"id":"cancel","label":"Cancel"}]}],
 "transcript":{"active":false,"partial":"","final":"dog को V-01 के पास भेजो","lang":"hi-IN","final_t":118.0},
 "tts":{"playing":{"text":"...","lang":"mr-IN","voice":"rohan","url":"/tts/ab12….wav","started_t":130.2}},
 "log":[{"t":118.0,"src":"STT|LLM|DET|NAV|TTS|LEAD|SIM|SYS","key":"stt_final","text":{"en-IN":"...","hi-IN":"...","mr-IN":"...","ta-IN":"..."}}],   // last 50
 "team":[{"id":"t1","name":"Rohan Deshmukh","role":"lead","lang":"hi-IN"}, ...]
}
{"type":"event","name":"stt.partial|stt.final|plan.proposed|plan.confirmed|plan.cancelled|detection|nav.arrived|tts.started|alert|sim.ready|sim.error|error","data":{...},"t":123.4}
{"type":"pong"}
```
Clients render only from `snapshot`; `event`s are for toasts, sounds and tests. Positions may be `null` when no pose has arrived yet.

## Mission plan JSON (sarvam-105b, strict schema; also produced by UI commands)
```json
{"intent":"aerial_survey|navigate_go2|speak|stop_all|land|unknown",
 "target":"V-01|null",
 "language":"hi-IN",
 "text_to_speak":"...|null",
 "confidence":0.0}
```
Executors: `aerial_survey` → drone survey; `navigate_go2` → planner + velocity controller to the target victim (or nearest with a life sign when null); `speak` → Bulbul phrase at the Go2 in the survivor's language (defaults to the nearest victim's language); `stop_all` → drone hold + Go2 stop, no confirmation needed; `land` → drone land. Every intent except `stop_all` produces a **plan decision** that must be confirmed.

## Module ownership (v1 work streams)
| Stream | Owner | Python package / dirs | Must not touch |
|---|---|---|---|
| A. Mission runtime + live state + WS server | sim-engineer-A | `skypaw/mission/{runtime,state,decisions,executors.py}`, `skypaw/live/{ws,snapshot}.py`, `skypaw/drone/*`, `skypaw/mission/coverage.py` (seen mask), `app.py` routes | `skypaw/go2`, `skypaw/perception`, `skypaw/voice`, `frontend/` |
| B. Detection | vision-engineer | `skypaw/perception/*`, `skypaw/sensors/*`, victim markers in `skypaw/scene/{site.yaml,markers.py}`, `scripts/detect.py` | everything else |
| C. Voice | voice-engineer | `skypaw/voice/*` (SarvamClient, realtime STT relay, planner, TTS cache, translate cache, credit meter), `scripts/checks/sarvam` | everything else |
| D. Console | frontend-engineer | `frontend/*` | `backend/` |
| E. Go2 rescue | sim-engineer-E | `skypaw/mission/grid.py`, `skypaw/mission/planner.py`, `skypaw/go2/*`, `scripts/go2_mission.py` | everything else |

Interfaces between streams are **Python protocols** declared in `skypaw/mission/ports.py` (owned by A, written first, tiny): `DroneExecutor.start_survey()/hold()/land()`, `Go2Executor.goto(victim_id)/stop()`, `Detector.on_frame(frame)→list[Candidate]`, `Voice.transcribe_stream()/plan(text,lang)/speak(text,lang)→url/translate(text,src,dst)`. Each stream implements its port and ships a fake implementation for others to test against.

## Budgets
Simulation: `duration ≤ 300 s` per run, always stopped in `finally`; each stream ≤ 10 requested minutes during development. Sarvam: ≤ ₹5 per stream during development; everything cached.

## Clarifications (resolved by streams D and E; binding for stream A)
- `grid.cls`: row-major bytes, `index = j*w + i`, row `j` at `y = y0 + (j+0.5)*cs` (row 0 = south edge), column `i` at `x = x0 + (i+0.5)*cs`. `grid.seen`: packed bits MSB-first (`numpy.packbits` order) over the same indexing; a server may instead send one byte per cell and the console accepts both.
- Decision actions: the console sends `confirm{plan_id}` for the primary/`go`/`confirm` action of a plan card, `cancel{plan_id}` for `cancel`, and `decide{decision_id, action}` for everything else (e.g. a speaker card's phrase ids).
- `command.intent = "stop_all"` may carry `target: "drone" | "go2"`; without a target it stops both. The drone's Hold button sends `stop_all` with target `drone`.
- The server sends a `snapshot` immediately after accepting `hello` (the console treats the first `snapshot`/`pong` as connected).
- Victims carry no language in v1; the console keeps a per-console survivor-language selector (default `mr-IN`) and sends it with `tts`.
- Camera overlay boxes are not drawn in v1 (no intrinsics in the snapshot).
