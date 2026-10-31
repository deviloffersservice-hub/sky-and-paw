# ADR-001 — Technology stack

**Status:** proposed, awaiting approval · **Date:** 2026-10-31 · **Deciders:** Fable (governor), project owner
**Inputs:** SPEC.md v1.1, docs/RESEARCH.md (Phase 0), docs/mockups/ops-v2.html

## Context

Sky & Paw is cloud-only and simulation-only. The backend must talk to Cyberwave (Python SDK, REST, MQTT) and Sarvam (REST + WebSocket), run YOLOv8n on frames at ≤5 Hz, keep one mission state machine (routing, triage, allocation, replanning), and push that state to a lead console and several phones in real time. The browser captures the lead's and teammates' microphones and plays Bulbul audio. Three facts from Phase 0 shape the choice:

1. The Cyberwave SDK, YOLO (`ultralytics`) and the Sarvam SDK are all Python-first.
2. MQTT on `mqtt.cyberwave.com:8883` is raw TLS; the development environment blocks it, and some PaaS hosts do too. The twin-state source must be pluggable (MQTT or REST polling).
3. The approved UI is a hand-built Canvas 2D map plus a Three.js twin with a precise visual contract (SPEC §5). Matching it exactly is easier by porting the mockup's rendering code than by re-expressing it in a component framework.

## Decision

| Layer | Choice | Pinned |
|---|---|---|
| Language / runtime | Python **3.11** | `python:3.11-slim` base image |
| Backend framework | **FastAPI** + **uvicorn** (ASGI; native WebSockets; async tasks for sim polling, Sarvam sockets and the mission loop) | fastapi 0.115.x, uvicorn[standard] 0.30.x, pydantic 2.x |
| Mission engine | Pure-Python package `skypaw.mission` (grid, Dijkstra planner, fusion, triage, allocation, replanning, decisions) ported from the mockup's engine, with the same constants as SPEC §6 | — |
| Cyberwave | `cyberwave` **0.7.4**; `TwinStateSource` interface with `MqttStateSource` (paho, 8883) and `RestPollingStateSource` (2–5 Hz) selected by env var `CW_STATE_SOURCE=mqtt|rest`; frames via `camera.get_frame("numpy")` (REST `latest-frame`) | cyberwave==0.7.4, paho-mqtt (SDK dep) |
| Perception | **ultralytics YOLOv8n** on CPU (640 px, ~80–150 ms/frame on 2 vCPU), OpenCV headless, NumPy; synthetic sensors in `skypaw.sensors` | ultralytics 8.3.x, opencv-python-headless 4.10.x, torch CPU wheels |
| Voice / language | `sarvamai` **0.1.41** behind one `SarvamClient` with retries, timeouts, credit meter; realtime STT relayed over the backend WebSocket; TTS WAV cached on disk under `data/tts/<sha1>.wav`; translations cached in SQLite | sarvamai==0.1.41 |
| State / persistence | In-memory mission state (single process) + **SQLite** (aiosqlite) for mission log, comms, translation cache, evidence index; no Redis, no Postgres | aiosqlite 0.20.x |
| Realtime to browsers | **One FastAPI WebSocket** per client: JSON state snapshots at 5 Hz plus event messages; binary frames upstream for microphone PCM (16 kHz mono int16, 100 ms chunks) | — |
| Frontend | **Vite 5 + TypeScript**, no UI framework; the mockup's Canvas 2D map, decision queue, panels and phone view ported module-by-module; **Three.js** r160-class pinned for the 3D twin; built assets served by FastAPI from `/static` | vite 5.4.x, typescript 5.6.x, three 0.169.x |
| Browser audio | Web Audio API: `AudioWorklet` capture → int16 PCM → WebSocket; TTS playback via `<audio>` from `/tts/<id>.wav` after a user gesture | — |
| Packaging / run | Single **Docker** image (backend + built frontend + YOLO weights); `make run` (or `docker compose up`) runs everything locally; `.env.example` lists every variable | — |
| Hosting | **Hugging Face Spaces (Docker Space, free "CPU basic": 2 vCPU, 16 GB RAM, no card)**. One Space runs the whole image (backend + built frontend + YOLO weights) on port 7860 with WebSockets; secrets in Space settings; outbound network open so `CW_STATE_SOURCE=mqtt` in production, `rest` in the dev environment. Disk is ephemeral: SQLite and the TTS cache live in `/data` inside the container and are rebuilt on restart (TTS phrases are pre-warmed at startup from a committed phrase list to spend credits once per deploy). The free Space sleeps after 48 h without visitors; the demo script includes "open the Space 3 minutes before the demo". | Space SDK `docker`, `README.md` front-matter in repo |
| Cost | **₹0 / $0.** Open-weight YOLOv8n; Sarvam ₹100 signup credit (metered, cached); Cyberwave credits as available in the account; GitHub Actions free minutes; HF Spaces free tier. No paid service anywhere. | — |
| CI | GitHub Actions (free): lint (ruff), unit tests (pytest), frontend build, Playwright UI tests; deploy on push to `main` by pushing the repo to the Space's git remote with `HF_TOKEN` | — |
| Secrets | `CYBERWAVE_API_KEY`, `CYBERWAVE_ENVIRONMENT_ID`, `SARVAM_API_KEY`, `SKYPAW_LEAD_TOKEN`, `SKYPAW_TEAM_TOKEN` from environment only | — |

### Request flow
```
Browser (lead/phone) ──WS JSON+PCM──► FastAPI ──► Sarvam realtime STT (WSS) ──► transcript events
                                      │ ├──► sarvam-105b (REST) ──► plan JSON ──► decision queue
                                      │ ├──► bulbul:v3 (REST, cached) ──► /tts/*.wav ──► browser <audio>
                                      │ └──► sarvam-translate:v1 (REST, cached)
                                      ├──► Cyberwave SDK/REST: twins, sim start/stop, waypoints, navigation, alerts, latest-frame
                                      ├──◄ TwinStateSource: MQTT 8883 (prod) | REST polling (dev)
                                      └──► Perception worker (asyncio.to_thread): YOLOv8n + synthetic sensors → fusion → victims/waypoints
```

## Rejected options

| Option | Why not |
|---|---|
| Fly.io / Railway / Render paid tiers | Project constraint: **no paid services**. Fly requires a card and bills per machine; the others' usable tiers are paid. |
| Render / Koyeb free tiers | 512 MB RAM is below what PyTorch + YOLOv8n + OpenCV need at import time; Render free sleeps after 15 min and kills the sim loop mid-mission. |
| Oracle Cloud Always-Free VM | Genuinely free and powerful, but sign-up needs a card for identity verification and a day of setup; keep as plan B if HF Spaces proves too slow. |
| Google Cloud Run free tier | Needs a billing account (card) even within the free quota. |
| Node/TypeScript backend | Cyberwave has only a preview JS SDK (0.1.0, "may not include preview APIs"); YOLO would need ONNX Runtime and a reimplementation of the SDK's REST/MQTT contracts. Python keeps three vendor SDKs first-class. |
| React / Next.js frontend | The approved UI is imperative canvas drawing with precise tokens; a component framework adds a translation layer without helping fidelity. Vite + TS keeps the mockup code nearly verbatim and typed. |
| Vercel / Netlify for the app | No long-lived Python WebSocket backend; serverless functions time out on sim polling and realtime STT relays. Could host the static frontend only, but one origin is simpler. |
| Google Cloud Run | WebSockets are capped at 60 min and connections are per-instance; YOLO cold starts with weights are slow; min-instances cost more than a Fly machine. Outbound 8883 works, but the trade is not worth it for a single-user demo. |
| Railway / Render | Equivalent to Fly for this workload; Render's free tier sleeps (kills the sim loop); Railway's trial credit runs out mid-hackathon. Fly's `bom` region is the tie-breaker for Sarvam latency. |
| Redis / Postgres | One process, one mission at a time; SQLite is enough and removes two services from the run command. |
| socket.io / MQTT-to-browser | Extra protocol layers; FastAPI's WebSocket already carries JSON and binary. Browsers never talk to Cyberwave's MQTT directly (keeps the API key server-side). |
| Running YOLO in the browser (ONNX/WebGPU) | Frames come from Cyberwave's REST into the backend anyway; running detection server-side keeps a single source of truth and avoids per-client duplication. |
| GPU hosting for YOLO | Nano on CPU at ≤5 fps is enough; a GPU box costs more than the whole hackathon budget. |

## Consequences
- Everything runs from one container with one command; a stranger follows README → `make run`.
- Dev in this cloud environment uses `CW_STATE_SOURCE=rest`; production on Fly uses `mqtt`. QA must run the live suite in both modes once.
- One free Space, one process, ephemeral disk: horizontal scaling and durable history are out of scope (one mission, one lead). The mission log is also written to the browser console/evidence files by QA, so nothing needed for judging depends on disk surviving a restart.
- Free-tier cold start after sleep is 1–3 minutes (image pull + YOLO weights load); the demo runbook wakes the Space in advance.
- Three.js is bundled by Vite rather than loaded from a CDN, so the deployed console has no third-party runtime dependency.

## Accounts and access the owner must provide (all free, no card)
1. **Hugging Face** account (free). Create a **Docker Space** named `sky-and-paw` (public or private), and a **write-scope access token**; add the token as the GitHub Actions secret `HF_TOKEN` and tell Fable the Space URL (`https://huggingface.co/spaces/<user>/sky-and-paw`). Fable adds `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` as Space secrets via the UI instructions in the README, or you paste them in *Settings → Variables and secrets*.
2. **Cyberwave** API key: DONE (in the environment). Please also confirm the **credit balance** in the dashboard; MuJoCo costs 0.01 credits/min and there is no documented free allocation.
3. **Sarvam** API key: DONE (in the environment). ₹100 free credit assumed.
4. **GitHub**: Actions allowed: DONE. `main` will be created from `claude/spec-and-mockup` on approval.
5. Network: the dev environment still blocks raw TLS on 8883 (its proxy is HTTPS-only), so dev uses `CW_STATE_SOURCE=rest`; the Space uses `mqtt`. No action needed.
