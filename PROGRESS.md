# PROGRESS.md — Sky & Paw

Governor: Fable. One entry per phase/milestone/task: what is done, the evidence, and what is next.

## Phase 0 — Verify reality · DONE (2026-10-31)
- **Done:** six subagents defined (`.claude/agents/`); Cyberwave and Sarvam facts verified from current SDK source and docs; network reachability tested; `docs/RESEARCH.md` written.
- **Evidence:** `docs/RESEARCH.md` (tables with status per claim and sources); local introspection of `cyberwave==0.7.4` and `sarvamai==0.1.41`.
- **Blocker resolved:** `dji/mini-4-pro` is not MuJoCo-compatible → owner approved `holybro/px4vision`. SPEC.md bumped to v1.1 with the corrections in RESEARCH.md §4; mockup labels updated.
- **Open items carried forward:** API keys not yet in the environment (D3); Cyberwave credit balance unverified (D4); MQTT 8883 blocked in dev (D2 → ADR-001); GLB collision readiness (D6 → M1 task).

## Phase 1 — Stack decision · APPROVED (2026-10-31)
- **Done:** `docs/ADR-001-stack.md` **approved by owner** (FastAPI + Python 3.11, Vite + TypeScript + Three.js, one WebSocket, SQLite). Hosting revised twice under the zero-cost, no-card constraint: Fly.io rejected (card); HF Docker Spaces rejected (now paid); final: **backend in GitHub Codespaces free tier, frontend on GitHub Pages**.
- **Done by owner:** `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` in the environment; GitHub Actions allowed; Cyberwave balance **50 credits** (enforcement on, warning <10; QA floor 15).
- **Done:** `main` created from `claude/spec-and-mockup`. Task T0 (scaffold) written and assigned.
- **Budget rule for QA:** every live run passes `duration ≤ 600 s` and stops the sim in `finally`; abort live tests if balance < 15 credits; Sarvam spend logged per run.
- **Next:** M1 (scene) on `feat/m1-scene`, then M2.

## Day-one live checks · DONE (2026-10-31)
- Cyberwave rounds 1–2 (≈0.07 credits): px4vision crashes MuJoCo → drone is `skydio/skydio-x2` (SPEC v1.2); MQTT works over WSS 443; Go2 moves on velocity commands, `goto` inert in unmapped scenes; custom GLB → MuJoCo via raw upload + asset PATCH. See `docs/RESEARCH.md` §6, `docs/evidence/day1/cyberwave.md`.
- Sarvam: key authenticates, zero credits on the key visible to this session; owner rotated to another account's key, which only a fresh session/Codespace can read. Scripts in `scripts/checks/sarvam/`. M5 live tests run there.

## Deadline package (owner request, due 15:45 IST 2026-10-31)
- Scope cut approved by owner: deliver (1) the approved interactive console on GitHub Pages, (2) a real Cyberwave simulation run with captured frames shown in the console, (3) DEMO.md stating live vs simulated. M2–M7 continue afterwards.
- Pages: owner set Source = GitHub Actions. Deploys from `main` still failed instantly because the `github-pages` environment only allows the repository's **default branch**, which is `claude/spec-and-mockup`. Workaround: `main` is mirrored to that branch and the workflow also triggers on it. **Live since 14:46 IST:** https://deviloffersservice-hub.github.io/sky-and-paw/ (verified headlessly: mission runs, 4 real frames served). To remove the workaround later: Settings → Branches → default branch = `main`.
- M2 probe merged: drone takeoff + lateral flight to the footprint centre at 6 m over MQTT-WSS velocity bursts (10 s), overhead bottom-camera frame shows the rubble (mean 150, std 50); Go2 moved 0.8 m on `move_forward`. 0.02 credits.

## Milestones
| # | Milestone | Status | Evidence |
|---|---|---|---|
| T0 | Repository scaffold (backend, frontend shell, Makefile, Docker, devcontainer, CI, Pages) | **DONE** · merged `main` 2026-10-31 | `tests/evidence/T0/REPORT.md` |
| M1 | Scene: environment + GLB + drone + Go2 by idempotent script | **DONE (demo-grade)** · merged 2026-10-31 · live: MuJoCo ready in 48–55 s, 3 cameras captured, MQTT-WSS pose, clean stop, 0.035 credits · **follow-up merged:** repeat run = 0 writes (asset found via twin or `assets.search`), orphan deleted, 15 offline tests (107 total) · **remaining gaps:** RUNBOOK, A6/A7-abort not run live, formal review+QA loop skipped for the 15:45 deadline | `tests/evidence/M1/dev/` |
| M2 | Drone mission: takeoff, scan, camera down, frame capture | not started | — |
| M3 | Vision: YOLO → waypoints with priority + alerts | not started | — |
| M4 | Dog mission: Go2 to waypoints by priority; time-to-rescue | not started | — |
| M5 | Voice: commands → plan → execution; Bulbul; translated log | not started | — |
| M6 | Dashboard per mockup, live, deployed | not started | — |
| M7 | Hardening, README, demo script, recording plan | not started | — |

## v1 build (owner: "real working website with voice and robots", started 14:52 IST 2026-10-31)
Scope cut applied for v1 (owner asked to finish asap): no 3D twin, egress/EVERYONE OUT, team positioning, man-down, body cams, phone app, allocation timeline. Contract: `docs/PROTOCOL.md`; ports: `backend/skypaw/mission/ports.py`.
Five parallel streams, each on its own branch and worktree, 120-minute time box:
| Stream | Branch | Owner | Scope |
|---|---|---|---|
| A | `feat/v1-runtime` | sim-engineer | mission runtime, live state, WebSocket server, drone survey executor |
| B | `feat/v1-detection` | vision-engineer | victim markers in the scene, YOLO + marker detector, projection, fusion, waypoints/alerts |
| C | `feat/v1-voice` (+`-2`) | voice-engineer | **MERGED.** SarvamClient, realtime STT relay, planner chain (`sarvam-105b-conversations` → `sarvam-105b` → rules), Bulbul cache, translate cache. **Live-verified via GitHub Actions with the new key** (plan 404 ms, realtime STT works, ₹0.20/run). |
| D | `feat/v1-console` | frontend-engineer | **MERGED.** console on live snapshots, mic capture + 16 kHz PCM, TTS playback, mock server, 20 Playwright e2e tests |
| E | `feat/v1-go2` (+`-3`,`-4`) | sim-engineer | **MERGED, LIVE-VERIFIED: Go2 reached V-01 in 100 s (0.63 m), 22.4 m route, no oscillation.** Part 1 (grid, planner, controller, scoring; controller followed 14 m live). Go2 stopped 8 m short at an invisible obstacle: rubble mesh placement in MuJoCo ≠ site.yaml (centroid recentring suspected). Part 2 in progress: read the server's MuJoCo XML, fix build_glb/placement, re-verify. |
Integration after all five land: merge A → E → B → C → D, wire real executors, one live end-to-end run, QA, deploy (Codespace backend + Pages console).
