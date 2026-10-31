# T0 — Repository scaffold

**Owner:** general engineer (opus) · **Branch:** `feat/t0-scaffold` from `main` · **Reviewer:** reviewer · **QA:** qa-engineer (smoke only)

## Goal
A runnable skeleton that every milestone builds on: Python backend (FastAPI) with the mission package layout, Vite + TypeScript frontend shell, one command to run everything, Docker image for a Hugging Face Docker Space, CI, env handling. No feature logic yet beyond health endpoints and a static page.

## Files to create
```
backend/
  pyproject.toml            # project metadata; deps pinned exactly (see list)
  requirements.txt          # pinned, generated from pyproject (pip-compile style)
  skypaw/__init__.py
  skypaw/config.py          # pydantic-settings: CYBERWAVE_API_KEY, CYBERWAVE_ENVIRONMENT_ID, SARVAM_API_KEY, CW_STATE_SOURCE=rest|mqtt, SKYPAW_LEAD_TOKEN, SKYPAW_TEAM_TOKEN, DATA_DIR=/data, PORT=7860, SIM_MAX_DURATION_S=600, CREDIT_FLOOR=15
  skypaw/logging.py         # structured logger (json lines), used everywhere instead of print
  skypaw/app.py             # FastAPI app factory: /healthz, /version, /ws (echo stub), serves frontend/dist at /
  skypaw/sim/guard.py       # assert_simulation_only(): refuses any Cyberwave affect mode other than simulation/playground; raises on "live", "real-world", "real", "tele"
  skypaw/sim/__init__.py, skypaw/perception/__init__.py, skypaw/voice/__init__.py, skypaw/mission/__init__.py
  tests/test_health.py, tests/test_guard.py
frontend/
  package.json (pinned exact versions), vite.config.ts, tsconfig.json, index.html, src/main.ts (renders "Sky & Paw Ops · connecting…" and opens /ws), src/styles.css (tokens copied from docs/mockups/ops-v2.html :root)
.devcontainer/devcontainer.json  # image mcr.microsoft.com/devcontainers/python:3.11 + node 20 feature; postCreateCommand "make setup"; postStartCommand "make run"; forwardPorts [7860] with portsAttributes visibility "public", label "Sky & Paw backend"; hostRequirements cpus 2; containerEnv CW_STATE_SOURCE=mqtt, DATA_DIR=/workspaces/sky-and-paw/data
Dockerfile                  # optional generic image: multi-stage node build → python:3.11-slim; CMD uvicorn on $PORT (7860); non-root; HEALTHCHECK /healthz (Docker is free; not used for hosting)
Makefile                    # make setup | make run | make test | make build | make docker
.env.example                # every variable from config.py with comments, no values
.gitignore                  # .env, data/, node_modules, dist, __pycache__, .venv, tests/evidence/**/*.wav
.github/workflows/ci.yml    # on push/PR: ruff, pytest, npm ci + typecheck + build, playwright smoke (page loads, /healthz 200)
.github/workflows/pages.yml # on push to main: build frontend with VITE_BASE=/sky-and-paw/ and deploy to GitHub Pages (actions/configure-pages with enablement: true, upload-pages-artifact, deploy-pages)
README.md                   # how to run from scratch in 5 commands; Codespaces one-click section; architecture diagram from ADR-001; link to SPEC.md
```

## Pinned versions (use exactly these or the latest patch of the same minor if a pin is unavailable, and record what you used)
fastapi 0.115.x · uvicorn[standard] 0.30.x · pydantic 2.9.x · pydantic-settings 2.5.x · aiosqlite 0.20.x · httpx 0.27.x · cyberwave 0.7.4 · sarvamai 0.1.41 · numpy 1.26.x · opencv-python-headless 4.10.x · ultralytics 8.3.x (CPU torch wheels from the pytorch CPU index) · ruff 0.6.x · pytest 8.3.x · pytest-asyncio 0.24.x · vite 5.4.x · typescript 5.6.x · three 0.169.x · @playwright/test 1.47.x (dev only).

## Hard rules
- No secrets in files. `.env.example` only. Config comes from environment variables.
- `assert_simulation_only()` must be importable and unit-tested; every later Cyberwave call site will use it.
- Single command `make run` starts backend on :7860 serving the built frontend; `make test` runs ruff + pytest + frontend typecheck.
- Dockerfile must build in this environment (`docker` may be unavailable here: if so, verify the Dockerfile by reasoning and by `hadolint`-style review, and state that it was not built).

## Acceptance criteria
- A1 `make setup && make test` passes locally with no network calls to Cyberwave or Sarvam.
- A2 `make run` serves `GET /healthz` → `{"status":"ok","version":...}` and `GET /` → the frontend shell; `/ws` accepts a connection and echoes a `{"type":"hello"}` message.
- A3 `python -c "from skypaw.sim.guard import assert_simulation_only; assert_simulation_only('live')"` raises `SimulationOnlyViolation`; `'simulation'` and `'playground'` pass.
- A4 CI workflow is valid YAML and runs the same steps as `make test` plus a Playwright smoke test; the Pages workflow is valid YAML and the frontend build honours `VITE_BASE`.
- A7 The frontend shell reads the backend URL from `?api=<url>` or `localStorage.skypaw_api`, defaulting to same-origin, and the backend sets CORS for `https://deviloffersservice-hub.github.io` and `*.app.github.dev`.
- A8 `.devcontainer/devcontainer.json` is valid JSON with the fields above (verified by `python -m json.tool`).
- A5 README lets a stranger run it: clone → copy `.env.example` → `make setup` → `make run` → open http://localhost:7860.
- A6 `.gitignore` excludes `.env` and `data/`; `git status` after `make run` shows no generated files.

## Test plan
- Unit: pytest for health, version, guard (including case-insensitivity and aliases `sim`, `mujoco`).
- Smoke: start server, curl /healthz, open / with Playwright, assert title "Sky & Paw Ops".
- Evidence: tests/evidence/T0/REPORT.md with command output and a screenshot of the shell page.
