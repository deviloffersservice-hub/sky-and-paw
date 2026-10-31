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
| Hosting: backend | **GitHub Codespaces (free tier: 120 core-hours + 15 GB/month for personal accounts, no card)**. A `.devcontainer` in the repo builds the environment, `postCreateCommand` runs `make setup`, `postStartCommand` runs `make run`, and port 7860 is forwarded with **public** visibility, giving a `https://<codespace>-7860.app.github.dev` URL that carries HTTP and WebSockets. Outbound network is open, so `CW_STATE_SOURCE=mqtt` there and `rest` in the Claude dev environment. Secrets are Codespaces repository secrets (`CYBERWAVE_API_KEY`, `SARVAM_API_KEY`). Idle timeout is raised to 4 h in the devcontainer settings. A 2-core machine gives ~60 hours/month: enough for QA runs and the demo. The demo runbook: open the Codespace 3 minutes before, copy the forwarded URL into the console's "backend" field once. | `.devcontainer/devcontainer.json` |
| Hosting: frontend | **GitHub Pages** (free) at `https://deviloffersservice-hub.github.io/sky-and-paw/`, deployed by Actions on push to `main`. The console reads the backend URL from `?api=` or a settings field (stored in `localStorage`) and defaults to same-origin when the backend serves it. Optional mirror: a free **Hugging Face Static Space** (static only; HF now charges for Docker/Gradio Spaces). | `.github/workflows/pages.yml` |
| Cost | **₹0 / $0, no card anywhere.** Open-weight YOLOv8n; Sarvam ₹100 signup credit (metered, cached); Cyberwave prepaid balance 50 credits (MuJoCo 0.01/min, enforcement on, warning below 10 → QA stops at 15); GitHub Actions, Codespaces and Pages free tiers. Docker the tool is free and used only for the optional image build. | — |
| CI | GitHub Actions (free): lint (ruff), unit tests (pytest), frontend typecheck + build, Playwright smoke; Pages deploy of the frontend on push to `main`. No backend deploy step: the backend runs in the Codespace. | — |
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
| Hugging Face Docker / Gradio Spaces | **Paid since 2026** ("Gradio and Docker Spaces require a paid plan. Static Spaces stay free"). Only a Static Space is free, so HF can host the frontend mirror at most. |
| Fly.io / Railway / Render paid tiers | Project constraint: **no paid services**. Fly requires a card and bills per machine; the others' usable tiers are paid. |
| Running the backend only on the owner's laptop | Free and simplest, but the brief asks for a cloud host and judges may open the console from their own devices. Kept as the emergency fallback (`make run` works identically). |
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
- One Codespace, one process: horizontal scaling and always-on hosting are out of scope (one mission, one lead). The Codespace disk persists between stops (15 GB quota), so SQLite and the TTS cache survive restarts; the monthly core-hour quota is the real limit, tracked in PROGRESS.md.
- Cold start of a stopped Codespace is ~1–2 minutes (container resume + YOLO weights load); the demo runbook opens it in advance. The frontend on Pages is always up.
- The frontend and backend are on different origins in production, so the backend enables CORS for the Pages origin and the console carries a backend-URL setting.
- Three.js is bundled by Vite rather than loaded from a CDN, so the deployed console has no third-party runtime dependency.

## Accounts and access (all free, no card)
1. **GitHub** (DONE): repo connected, Actions allowed, `main` created. Fable's remaining asks, each a one-time click in the repo settings when the time comes: add `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` as **Codespaces secrets** (Settings → Secrets and variables → Codespaces), and open the Codespace once before each demo/QA run.
2. **Cyberwave** (DONE): key in the environment; balance **50 credits**, enforcement on, warning below 10.
3. **Sarvam** (DONE): key in the environment; ₹100 free credit assumed.
4. **Hugging Face** (optional): the owner's existing account can host a free Static Space mirror of the frontend later; nothing needed now.
5. Network: the dev environment blocks raw TLS on 8883 (HTTPS-only proxy), so dev uses `CW_STATE_SOURCE=rest`; the Codespace uses `mqtt`. No action needed.
