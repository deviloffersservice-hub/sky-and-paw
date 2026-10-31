# PROGRESS.md — Sky & Paw

Governor: Fable. One entry per phase/milestone/task: what is done, the evidence, and what is next.

## Phase 0 — Verify reality · DONE (2026-10-31)
- **Done:** six subagents defined (`.claude/agents/`); Cyberwave and Sarvam facts verified from current SDK source and docs; network reachability tested; `docs/RESEARCH.md` written.
- **Evidence:** `docs/RESEARCH.md` (tables with status per claim and sources); local introspection of `cyberwave==0.7.4` and `sarvamai==0.1.41`.
- **Blocker resolved:** `dji/mini-4-pro` is not MuJoCo-compatible → owner approved `holybro/px4vision`. SPEC.md bumped to v1.1 with the corrections in RESEARCH.md §4; mockup labels updated.
- **Open items carried forward:** API keys not yet in the environment (D3); Cyberwave credit balance unverified (D4); MQTT 8883 blocked in dev (D2 → ADR-001); GLB collision readiness (D6 → M1 task).

## Phase 1 — Stack decision · PROPOSED
- **Done:** `docs/ADR-001-stack.md` written (FastAPI + Python 3.11, Vite + TypeScript + Three.js, one WebSocket, SQLite, single Docker image). Hosting revised to **Hugging Face Spaces free Docker tier** after the owner's zero-cost constraint; Fly.io rejected.
- **Done by owner:** `CYBERWAVE_API_KEY` and `SARVAM_API_KEY` in the environment; GitHub Actions allowed.
- **Waiting on owner:** approval of revised ADR-001; HF Space + `HF_TOKEN`; Cyberwave credit balance.
- **Next:** on approval, create `main` from `claude/spec-and-mockup`, scaffold the repo (backend, frontend, Makefile, Dockerfile, `.env.example`, CI), then start M1.

## Milestones
| # | Milestone | Status | Evidence |
|---|---|---|---|
| M1 | Scene: environment + GLB + drone + Go2 by idempotent script | not started | — |
| M2 | Drone mission: takeoff, scan, camera down, frame capture | not started | — |
| M3 | Vision: YOLO → waypoints with priority + alerts | not started | — |
| M4 | Dog mission: Go2 to waypoints by priority; time-to-rescue | not started | — |
| M5 | Voice: commands → plan → execution; Bulbul; translated log | not started | — |
| M6 | Dashboard per mockup, live, deployed | not started | — |
| M7 | Hardening, README, demo script, recording plan | not started | — |
