# Sky & Paw: brief for making the demo video

This file is the complete context for a fresh Claude Code session whose job is to produce a
demo video of Sky & Paw. Read it fully before doing anything. Everything here was verified on
2026-10-31 against `main`.

## 1. What the product is

Sky & Paw is a simulation-only, multilingual disaster-response console built for the Ship
Happens hackathon (track 02 "Hardwired", Cyberwave; Sarvam AI credits).

- A **drone twin** (Skydio X2) flies a lawnmower survey over a collapsed 3-floor building
  ("Ghatkopar, Block C", 18 × 12 m) and maps it. Its camera frames feed a person detector, and
  candidate victims appear on the map with fused evidence and a suggested triage
  (V-01 immediate, V-02 delayed, V-03 unknown, D-01 expectant decoy).
- A **Unitree Go2 twin** (robot dog) is sent to a victim. It plans a route on the mapped grid
  and walks there with closed-loop velocity bursts.
- The **field lead speaks in Indian languages** (Hindi, Marathi, Tamil, English, code-mixed).
  Sarvam handles speech-to-text (saaras), turns the sentence into a strict JSON mission plan
  (sarvam-105b), speaks to the survivor through the Go2 speaker (bulbul TTS, cached), and
  translates the mission log for each teammate (sarvam-translate).
- **Nothing moves without the lead confirming a plan card.** "Stop all" is the only command
  that skips confirmation.
- Everything robotic runs in **Cyberwave's cloud MuJoCo simulation**. Every Cyberwave call goes
  through one guard that only allows simulation mode.

One-line pitch: "One person directs a drone and a robot dog in their own language, and every
robot action is a plan they confirm."

## 2. What is real today (say this honestly in the video)

| Part | Status |
|---|---|
| Cyberwave MuJoCo scene: rubble split into 18 collision pieces, drone, Go2, waypoints | Live, created by `scripts/scene.py` |
| Drone survey: takeoff, 10 corners, ~70 frames, 100% mapped | Live-verified, about 190 s in the real sim |
| Go2 route + walk to V-01 | Live-verified, 50 s, 0.7 m from the victim, no rubble pushed |
| Voice: realtime STT, plan, TTS, translate | Live-verified against Sarvam (cost about ₹0.20 per run) |
| Console wired to the backend over one WebSocket (5 Hz snapshots) | Live |
| Victim detection on `main` | Offline/synthetic detector with fused synthetic sensors. A real YOLO detector exists on branch `feat/v1-integration` (not merged) |
| Hosting | Console on GitHub Pages, backend in a GitHub Codespace, ₹0 |

Links:
- Repository: https://github.com/deviloffersservice-hub/sky-and-paw
- Static mockup page: https://deviloffersservice-hub.github.io/sky-and-paw/
- Live console shell: `https://deviloffersservice-hub.github.io/sky-and-paw/app.html?api=<backend URL>`

## 3. Repository map (only what the video needs)

| Path | What it is |
|---|---|
| `scripts/mission.py` | Starts the whole backend: mission runtime, robots, voice, web server on port 7860 |
| `backend/skypaw/` | Python backend (FastAPI). `app.py` serves the built console at `/` |
| `frontend/` | Vite + TypeScript + Three.js console. `make setup` builds it into `frontend/dist` |
| `docs/PROTOCOL.md` | The WebSocket / HTTP contract, incl. every command message |
| `SPEC.md` | Product spec and UI description |
| `docs/mockups/ops-v2.html` | The approved UI design, useful for intro shots |
| `tests/evidence/` | Real MuJoCo frames and logs from live runs (see section 7) |

## 4. Setup in a fresh cloud session

```bash
make setup          # Python venv (3.11+), npm ci, frontend build. Idempotent.
make test           # optional sanity check: ruff + 385 pytest tests + typecheck
```

- Chromium and Playwright are pre-installed in Claude Code cloud containers:
  `require('playwright')` works from any directory and `chromium.launch()` needs no options.
  **Do not run `playwright install`.**
- For video conversion, check `ffmpeg -version`. If it is missing, `pip install imageio-ffmpeg`
  gives a static ffmpeg binary (`python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"`).

## 5. Running the demo backend

### Option A: offline, real-time, free (recommended for the main recording)

No API keys and no credits. The drone and Go2 are kinematic stand-ins driven by the **real**
controllers, moving at real speed (`--realtime`).

```bash
.venv/bin/python scripts/mission.py --offline --realtime --autostart-survey \
  --serve --port 7860 --duration 900 --linger 900
```

Then open `http://127.0.0.1:7860/` (the backend serves the console). Verified timings:

| Step | Time |
|---|---|
| Drone survey (starts automatically), 4 victims appear | about 90 s |
| Go2 walks to V-01 after the plan is confirmed | about 33 s |
| Speaker phrase to the survivor | instant |

Limitations of option A, to handle in editing:
- The "Drone sees" / "Go2 sees" tiles show synthetic grey grid frames. Cut to the real MuJoCo
  frames from `tests/evidence/` for camera shots (section 7).
- Offline voice is a keyword planner and produces **no audio** (the TTS URL is a placeholder).
  For real survivor audio, use option B, or call Sarvam TTS once and add the WAV in editing.
- Commands must include the keywords the offline planner knows: survey/map/drone/ड्रोन/मैप for
  the survey, dog/go2/send/भेजो for the Go2.

### Option B: live Cyberwave + Sarvam (real sim and real voice)

Needs `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` in the session environment (the user sets them
in the cloud environment settings; never print, log or commit them; `.env` is gitignored).

```bash
.venv/bin/python scripts/mission.py --serve --port 7860 --real-go2 --real-voice \
  --env skypaw-v1-fast --autostart-survey --duration 900
```

- Use environment `skypaw-v1-fast` (20 twins, loads in about 60 s). Avoid `skypaw-v1-dev` and
  `skypaw-v1-dev-b`: they currently fail to load in Cyberwave ("Too many open files" / stuck
  loading). `skypaw-v1-dev-e` is the user's own presentation environment: do not use it
  while the user may be running it.
- Wait for `"sim": "ready"` in the log (45 to 60 s) before recording.
- Cost: Cyberwave 0.01 credits per simulated minute (900 s = 0.15 credits); Sarvam well under
  ₹1 per run. Stop the process when done: the simulation is stopped in `finally` and also ends
  itself when `--duration` runs out.

## 6. Driving the scenario

A cloud browser has no microphone, so use typed commands. Either click the small arrow next
to the HOLD button in the console and type into "Type a command…", or send them over HTTP
(the console shows the result exactly as if it had been spoken):

```bash
B=http://127.0.0.1:7860
# 1. (only if not using --autostart-survey) survey, then confirm its plan
curl -s -H 'content-type: application/json' $B/api/command \
  -d '{"type":"text_command","text":"ड्रोन भेजो, पूरी इमारत मैप करो","lang":"hi-IN"}'
# read the open plan id from the snapshot
curl -s $B/api/state | python3 -c "import json,sys;s=json.load(sys.stdin);print([d['plan']['id'] for d in s['decisions'] if d.get('plan')])"
curl -s -H 'content-type: application/json' $B/api/command -d '{"type":"confirm","plan_id":"P-1"}'
# 2. after victims appear: send the dog to V-01 (Hindi), then confirm the new plan id
curl -s -H 'content-type: application/json' $B/api/command \
  -d '{"type":"text_command","text":"dog को V-01 के पास भेजो","lang":"hi-IN"}'
# 3. when the Go2 has arrived: speak to the survivor in Marathi
curl -s -H 'content-type: application/json' $B/api/command -d '{"type":"tts","phrase_id":"p_help","lang":"mr-IN"}'
# other phrase ids: p_hear, p_still, p_medic. Stop everything: {"type":"command","intent":"stop_all"}
```

`GET /api/state` returns the same JSON the console renders (`mission.phase`, `survey.pct_mapped`,
`victims`, `robots.go2.mode`, `decisions`). Poll it to know when to move to the next step
(survey done when `pct_mapped` ≥ 99 and phase is `search`; Go2 done when `robots.go2.mode` is
`arrived`). Clicking the plan card's "Confirm · Go" in the console works too and looks better
on camera.

## 7. Real footage available in the repo

| File | Shows |
|---|---|
| `tests/evidence/V1-A/f_000001.jpg`, `f_000040.jpg`, `f_000078.jpg` | Drone camera frames from the live survey |
| `tests/evidence/M2/dev/overhead_bottom.jpg`, `overhead_payload.jpg` | Overhead MuJoCo views of the site |
| `tests/evidence/M1/dev/drone_bottom.jpg`, `drone_payload.jpg`, `go2_front.jpg` | First live scene frames |
| `tests/evidence/V1-E/run5/go2_front_arrival.jpg` | Go2 camera on arrival at V-01 |
| `docs/mockups/ops-v2.html` | Full designed console (map layers, triage, phone view) |

## 8. Suggested video (about 2 minutes)

1. **Problem (10 s).** Title card: "After a collapse, the first hour decides who lives."
2. **Product (10 s).** The console, idle. "One lead, one map, a drone and a robot dog, in their
   own language."
3. **Voice to plan (15 s).** Type or show the Hindi survey command, the transcript, the plan
   card from sarvam-105b, the lead confirms.
4. **Drone survey (25 s, speed up 3-4×).** The map fills in, the drone moves, victims pop up
   with triage colours. Cut to a real MuJoCo drone frame.
5. **Dispatch the dog (20 s).** "dog को V-01 के पास भेजो", confirm, the Go2 walks the planned
   route around the rubble. Cut to the real Go2 arrival frame.
6. **Talk to the survivor (10 s).** Marathi phrase plays through the Go2 speaker
   ("मदत येत आहे. शांत राहा." = "Help is coming. Stay calm.").
7. **Proof (15 s).** "Real Cyberwave MuJoCo simulation, real Sarvam voice." Show the live frames
   and the mission log translated into Hindi, Marathi and Tamil.
8. **Close (10 s).** Repo link and the static demo page.

Use captions throughout (judges often watch muted). Record at 1920×1080.

## 9. Recording recipe (Playwright)

```js
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: 'video-out/', size: { width: 1920, height: 1080 } },
  });
  const page = await ctx.newPage();
  await page.goto('http://127.0.0.1:7860/');
  // drive the scenario here (HTTP commands from section 6, or page clicks), waiting on /api/state
  await page.waitForTimeout(5000);
  await ctx.close();            // the .webm is written on close
  await browser.close();
})();
```

- Playwright video has **no audio**. Add narration and the survivor TTS WAV with ffmpeg.
- Convert and speed up dull parts with ffmpeg (`setpts=PTS/3` for a 3× survey).
- Keep the console's own clock visible when you speed up, or caption "3× speed".

## 10. Rules for the video session

- Do not commit API keys, `.env`, or large media. Put the finished video in a gitignored folder
  and send it to the user as a file. Commit only scripts, if any, on a new branch.
- Do not push to `main` or merge anything without the user asking.
- Do not start Cyberwave simulations except through `scripts/mission.py` / `scripts/scene.py`
  (they enforce simulation mode and always stop the run).
- Do not claim things section 2 says are not live (for example real YOLO detection on `main`).
