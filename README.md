# Sky & Paw

A drone and a quadruped robot search a collapsed building together, guided by a field lead
who speaks to them in Indian languages. **Simulation only**: robots run as Cyberwave MuJoCo
twins, and every Cyberwave call goes through `skypaw.sim.guard.assert_simulation_only()`.

- Product spec: [SPEC.md](SPEC.md)
- Stack decision: [docs/ADR-001-stack.md](docs/ADR-001-stack.md)
- Approved UI mockup: [docs/mockups/ops-v2.html](docs/mockups/ops-v2.html)
- Progress: [PROGRESS.md](PROGRESS.md)

Status: **T0 scaffold**. Health endpoints, a WebSocket echo stub and the console shell.
Mission features arrive in milestones M1 to M7.

## Run it from scratch (5 commands)

Needs Python 3.11+ (3.11 is the target and is preferred by `make setup`; 3.13 also works and
gets numpy 2.1.3 instead of 1.26.4, which has no 3.13 wheels), Node 20+, `make`.

```bash
git clone https://github.com/deviloffersservice-hub/sky-and-paw.git
cd sky-and-paw
cp .env.example .env      # optional for T0: every value may stay empty
make setup                # .venv + pinned Python deps, npm ci, frontend build (idempotent)
make run                  # backend on :7860, serving the console at /
```

Then open <http://localhost:7860>. The header should read **Sky & Paw Ops · connected**.

Check it from a second terminal:

```bash
curl http://localhost:7860/healthz     # {"status":"ok","version":"0.1.0"}
curl http://localhost:7860/version
```

### Make targets

| Command | What it does |
|---|---|
| `make setup` | Creates `.venv` (prefers `python3.11`), installs `backend/requirements-dev.txt` and the backend in editable mode, runs `npm ci`, builds the frontend. Safe to re-run: a second run does nothing. |
| `make run` | Builds the frontend if missing or stale, then `uvicorn skypaw.app:create_app --factory` on `0.0.0.0:7860` (`PORT`, `HOST` override), serving `frontend/dist` at `/` (the app falls back to a one-line placeholder page when no build exists). Data goes to `./data` unless `DATA_DIR` is set in the environment. |
| `make test` | `ruff check` + `ruff format --check` + `pytest` + `tsc --noEmit`. Tests block every non-loopback socket, so they never call Cyberwave or Sarvam. |
| `make build` | Builds the console into `frontend/dist`. `VITE_BASE=/sky-and-paw/ make build` builds for GitHub Pages. The base used is recorded in `frontend/dist/.vite-base`; a later `make run` or `make setup` sees it differs from `/` and rebuilds for the backend automatically, so a Pages build never leaves `make run` serving a blank page. |
| `make smoke` | Builds, starts the backend, and runs the Playwright smoke test (`/healthz` 200, page title, WebSocket hello, `?api=` + CORS from a second origin). Screenshot in `frontend/test-results/`. |
| `make setup-ml` | **Optional for T0.** Installs the ML stack (ultralytics 8.3.253, torch 2.5.1 CPU, torchvision 0.20.1 CPU from the PyTorch CPU index) from `backend/requirements-ml.txt`. About 1 GB, ~75 s here; needed from milestone M3. Python 3.11/3.12 only (no torchvision 0.20.1 wheel for 3.13). `pip check` reports ultralytics wanting `opencv-python`: expected, `opencv-python-headless` provides `cv2`. |
| `make docker` | Builds the optional Docker image (`docker run -p 7860:7860 --env-file .env skypaw:dev`). Not used for hosting. |
| `make lock` | Regenerates `backend/requirements*.txt` from `backend/pyproject.toml` with pip-tools. |

Frontend development with hot reload: `make run` in one terminal, `cd frontend && npm run dev`
in another, open <http://localhost:5173> (Vite proxies `/healthz`, `/version` and `/ws` to :7860).

## Configuration

All configuration comes from environment variables (or a local, git-ignored `.env`).
[`.env.example`](.env.example) lists every variable with a comment; nothing secret is ever
committed. The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `CYBERWAVE_API_KEY`, `SARVAM_API_KEY` | empty | Vendor keys. In Codespaces, add them as Codespaces secrets. |
| `CW_STATE_SOURCE` | `rest` | `mqtt` where port 8883 is reachable (Codespaces), `rest` polling elsewhere. |
| `CW_RUNTIME_MODE` | `simulation` | Read by the simulation guard; `live`, `real-world`, `real`, `tele` are refused. |
| `SKYPAW_ENV` | `dev` | `dev` also allows `http://localhost:*` through CORS. |
| `PORT` / `DATA_DIR` | `7860` / `/data` | HTTP port and data directory. |

## Use it from GitHub Codespaces (one click, free tier)

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/deviloffersservice-hub/sky-and-paw?quickstart=1)

1. Once only: add `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` under the repository's
   **Settings → Secrets and variables → Codespaces**.
2. Open the Codespace (button above, 2-core machine). `.devcontainer/devcontainer.json` runs
   `make setup` on creation and `make run` on every start, and forwards port 7860 publicly.
3. Copy the forwarded URL from the **Ports** tab (label "Sky & Paw backend"), e.g.
   `https://<codespace>-7860.app.github.dev`. It serves the console itself, or:
4. Open the console on GitHub Pages and point it at the Codespace once:
   `https://deviloffersservice-hub.github.io/sky-and-paw/?api=https://<codespace>-7860.app.github.dev`.
   The URL is remembered in `localStorage.skypaw_api`; the **Backend** field in the header
   changes it later.

The backend allows CORS from `https://deviloffersservice-hub.github.io` and any
`https://<name>.app.github.dev` origin (one DNS label), plus `http://localhost:*` when
`SKYPAW_ENV=dev`. The Codespace runs with `SKYPAW_ENV=prod`, so `/docs` is off there.

Notes:
- **Idle timeout** is a per-user setting, not part of `devcontainer.json`: raise it (e.g. to
  4 hours) at <https://github.com/settings/codespaces> → "Default idle timeout".
- **First Pages deploy:** `pages.yml` asks `actions/configure-pages` to enable Pages
  (`enablement: true`). If that first run fails with a permissions error, set the repository's
  **Settings → Pages → Source = GitHub Actions** once and re-run the workflow.

## Architecture

From [ADR-001](docs/ADR-001-stack.md): one FastAPI process (Python 3.11) holds the mission
state and talks to Cyberwave and Sarvam; browsers connect over one WebSocket each. The console
is Vite + TypeScript (Canvas 2D map, Three.js twin), served by the backend or by GitHub Pages.

```
                 GitHub Pages (static console)          GitHub Codespace (free tier)
                 deviloffersservice-hub.github.io       https://<codespace>-7860.app.github.dev
                          │  ?api=<codespace URL>                      │
                          ▼                                            ▼
Browser (lead/phone) ──WS JSON+PCM──► FastAPI ──► Sarvam realtime STT (WSS) ──► transcript events
                                      │ ├──► sarvam-105b (REST) ──► plan JSON ──► decision queue
                                      │ ├──► bulbul:v3 (REST, cached) ──► /tts/*.wav ──► browser <audio>
                                      │ └──► sarvam-translate:v1 (REST, cached)
                                      ├──► Cyberwave SDK/REST: twins, sim start/stop, waypoints, navigation, alerts, latest-frame
                                      ├──◄ TwinStateSource: MQTT 8883 (prod) | REST polling (dev)
                                      └──► Perception worker (asyncio.to_thread): YOLOv8n + synthetic sensors → fusion → victims/waypoints
```

### Repository layout

```
backend/
  pyproject.toml              exact pins; requirements*.txt are pip-compile locks of it
  skypaw/app.py               FastAPI factory: /healthz, /version, /ws, static console at /
  skypaw/config.py            pydantic-settings (environment only)
  skypaw/logging.py           JSON-lines logger
  skypaw/sim/guard.py         assert_simulation_only() / SimulationOnlyViolation
  skypaw/{sim,perception,voice,mission}/   milestone packages
  tests/                      pytest (network blocked)
frontend/                     Vite + TypeScript console shell; tests/smoke.cjs (Playwright)
.devcontainer/                Codespaces definition
.github/workflows/            ci.yml (lint, tests, typecheck, build, smoke), pages.yml (deploy)
Dockerfile                    optional image: node build → python:3.11-slim, non-root
```

## Simulation-only guard

```python
from skypaw.sim.guard import assert_simulation_only
assert_simulation_only("simulation")   # ok; also "sim", "mujoco", "playground" (any case)
assert_simulation_only()               # reads CW_RUNTIME_MODE, default "simulation"
assert_simulation_only("live")         # raises SimulationOnlyViolation
```

## Pinned versions

Python: fastapi 0.115.14, uvicorn[standard] 0.30.6, pydantic 2.9.2, pydantic-settings 2.5.2,
aiosqlite 0.20.0, httpx 0.27.2, cyberwave 0.7.4, sarvamai 0.1.41, numpy 1.26.4 (2.1.3 on Python 3.13),
opencv-python-headless 4.10.0.84; dev: ruff 0.6.9, pytest 8.3.5, pytest-asyncio 0.24.0;
optional ML: ultralytics 8.3.253, torch 2.5.1+cpu, torchvision 0.20.1+cpu.
Frontend: vite 5.4.21, typescript 5.6.3, three 0.169.0, @types/three 0.169.0,
@playwright/test 1.47.2 (dev).
