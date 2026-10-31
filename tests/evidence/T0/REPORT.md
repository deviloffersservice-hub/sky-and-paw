# T0 — Repository scaffold: evidence report

**Branch** `feat/t0-scaffold`, final head `e4b552e` (13 commits), merged into `main` 2026-10-31.
**Engineer** general (opus) · **Reviewer** verdict after rework: all REQUEST CHANGES items fixed · **QA** independent run on `9dbd5db`: A1–A8 PASS, 3/3 runs, no flakiness; rework re-verified by the engineer (`make test` 92 passed, `make smoke` PASS) and spot-checked by Fable (workflow permissions, SHA pins, single `affect` path, CORS regex, secret scan).

| # | Criterion | Result | Evidence |
|---|---|---|---|
| A1 | `make setup && make test` offline | PASS | `qa/01-make-setup.log`, `qa/02-make-test.log`, `qa/03-make-test-offline-x3.log` (run in a no-network namespace, 3/3) |
| A2 | `/healthz`, `/`, `/ws` hello echo | PASS | `qa/07-A2-curl.txt`, `qa/08-A2-ws-hello.txt` |
| A3 | Simulation guard refuses `live`, accepts sim/playground | PASS | `qa/05-A3-guard.txt` |
| A4 | Workflows valid; CI mirrors `make test` + smoke; `VITE_BASE` honoured | PASS (not yet run on GitHub) | `qa/14-A4-A8-workflows-devcontainer.txt`, `qa/16-A4-ci-pages-base-step.txt`, `qa/15-smoke-pinned-playwright-1.47.2.log` |
| A5 | README quick start on a fresh copy | PASS | `qa/17-A5-readme-fresh-copy.txt`, `qa/a5-readme-flow-localhost7860.png` |
| A6 | `.env`, `data/` ignored; clean status after run | PASS | `qa/10-A6-gitstatus-after-run.txt`, `qa/19-A6-gitstatus-final.txt` |
| A7 | `?api=` / localStorage / same-origin; CORS allowlist | PASS | `qa/11-make-smoke-run1.log`, `qa/09-A7-cors-dev.txt`, `qa/13-A7-cors-prod.txt`, `qa/viewports/*.png` (1280/1500/360 px, no horizontal overflow) |
| A8 | devcontainer.json valid with required fields | PASS | `qa/14-A4-A8-workflows-devcontainer.txt` |
| — | Docker image builds and runs | PASS before digest pinning (engineer, 595 MB, non-root, healthy); not rebuilt after pinning (no daemon) | engineer report |

**Review findings and resolution (commits `b933cb3`…`e4b552e`):** Pages workflow least privilege (deploy job only, configure-pages moved); all actions pinned to commit SHAs, base images to tag+digest, devcontainer image/feature to exact versions; Codespaces CORS regex tightened with lookalike-origin tests; `/ws` catches `RecursionError`; API docs only in dev; `simulation_affect()` is the sole `cw.affect` caller with a duration cap and a grep test; devcontainer `SKYPAW_ENV=prod`; Makefile `run` depends on the build, portable `lock`, `clean` covers ruff cache; CI `push` limited to `main`; README notes on Codespaces idle timeout and Pages enablement; structured-log field collisions prefixed.

**QA defect D1 (low, fixed in `e4b552e`):** a Pages build left in `frontend/dist` made `make run` serve a blank page; the build now records its base in `dist/.vite-base` and the Makefile rebuilds on mismatch.

**Carried forward:** `?api=` host allowlist/confirm → M6; WebSocket Origin check + token in first message → first milestone that puts state on `/ws` (M2). Pages `enablement: true` may need a one-time Settings → Pages → Source = GitHub Actions.

**Versions pinned:** fastapi 0.115.14, uvicorn 0.30.6, pydantic 2.9.2, pydantic-settings 2.5.2, aiosqlite 0.20.0, httpx 0.27.2, cyberwave 0.7.4, sarvamai 0.1.41, opencv-python-headless 4.10.0.84, numpy 1.26.4 (<3.13) / 2.1.3 (3.13); dev ruff 0.6.9, pytest 8.3.5, pytest-asyncio 0.24.0; ML (optional, `make setup-ml`) ultralytics 8.3.253, torch 2.5.1+cpu, torchvision 0.20.1+cpu; frontend vite 5.4.21, typescript 5.6.3, three 0.169.0, @playwright/test 1.47.2; Node 20.18.0; Python 3.11.18-slim-bookworm.
