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
| Hosting | **Fly.io**: one app, one machine (`shared-cpu-2x`, 2 GB), region `bom` (Mumbai) for Sarvam latency; outbound TCP 8883 allowed so `CW_STATE_SOURCE=mqtt` in production, `rest` in the dev environment; Fly secrets for keys; persistent volume (1 GB) for SQLite and the TTS cache | flyctl, `fly.toml` in repo |
| CI | GitHub Actions: lint (ruff), unit tests (pytest), frontend build, Playwright UI tests; deploy on push to `main` with `flyctl deploy` | — |
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
- SQLite plus a Fly volume means a single machine; horizontal scaling is out of scope (one mission, one lead).
- Three.js is bundled by Vite rather than loaded from a CDN, so the deployed console has no third-party runtime dependency.

## Accounts and access the owner must provide before M1
1. **Fly.io** account with a payment method on file (required to create apps; expected spend < $10 for the hackathon), org name shared with Fable, and a `FLY_API_TOKEN` as a GitHub Actions secret.
2. **Cyberwave** account with an API key (`cyberwave.com/profile → Access`), a credit balance confirmed in the dashboard, and the key set as `CYBERWAVE_API_KEY` in this cloud environment's secrets and in Fly.
3. **Sarvam** account with an API key (`indus.sarvam.ai/key-management`), the ₹100 free credits visible, set as `SARVAM_API_KEY` in both places.
4. **GitHub**: this repository already connected; allow Actions to run; `main` branch created from `claude/spec-and-mockup` when you approve this ADR.
5. Optional: if the cloud environment's network policy can allow `mqtt.cyberwave.com:8883` (raw TLS), add it so dev and prod use the same state source.
