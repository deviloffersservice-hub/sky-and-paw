# Sky & Paw — demo script (Ship Happens, Hardwired track)

**Live demo page (free, GitHub Pages):** https://deviloffersservice-hub.github.io/sky-and-paw/
**Repository:** https://github.com/deviloffersservice-hub/sky-and-paw

## What is real and what is simulated — say this first
| Part | Status today |
|---|---|
| Cyberwave MuJoCo simulation of the site: Unitree Go2 twin, Skydio X2 drone twin, custom rubble GLB, environment and waypoints created by our idempotent script (`make scene`) | **Live.** Frames captured from the running simulation are shown on the demo page ("Real Cyberwave MuJoCo frames") and committed under `tests/evidence/M1/`. Pose streams over MQTT-over-WebSocket. |
| Operator console: map with sensor layers, triage, allocation timeline, replanning on collapse, egress / EVERYONE OUT, request inbox, Go2 speaker, multilingual phone view | **Interactive mockup** driving a scripted mission (the approved design, `docs/mockups/ops-v2.html`). Every control works; the data is generated client-side. |
| Voice (Sarvam saaras STT → sarvam-105b plan → bulbul TTS → sarvam-translate) | **Designed and verified against the API contract**, simulated in the UI. Live calls were blocked today by a credit-less key (see `docs/evidence/day1/sarvam.md`); scripts to run them are in `scripts/checks/sarvam/`. |
| YOLO detection, fusion, Go2 navigation, dashboard wired to the live backend | **Specified** (`SPEC.md`, `docs/tasks/M2…M3`) and scaffolded (`backend/`, `frontend/`); not yet built. |

## 2-minute walkthrough
1. **Open the page.** Point at the header: time since collapse, triage counts, EVERYONE OUT. Say: "One human directs robots and people from one map; nothing moves without a confirmed plan."
2. **Tick `auto`, pick `4×`, press ▶ Play demo.** Narrate as it runs (≈50 s):
   - The lead speaks Hindi; partial transcript streams in; a **plan card** appears; it is confirmed; the drone maps the building (passability fills in).
   - **Candidates** appear with sensor evidence; the decoy D-01 is thermal-only and flagged *Expectant*.
   - **Allocation proposal** with a timeline: medic first, extraction pair after stabilisation, Go2 to the top victim. Approve.
   - A **secondary collapse** blocks a route → reroute; a teammate goes **silent** → man-down check; V-02 **weakens** → triage raised and replanned; teammate talks back in Tamil and requests drone overwatch.
3. **Click anything.** A victim pin → triage bar, needs, send buttons, evidence. Empty map → dispatch menu (people go immediately, bots ask to confirm). A camera tile → focus mode.
4. **Phone view.** Switch to Priya (Marathi): the lead's words translated, her task and way out, requests, hold-to-talk.
5. **Point at the evidence strip** (bottom-right): "These three frames are not rendered by the page; they came out of Cyberwave's MuJoCo simulation this afternoon from our scene script." Open one.
6. **Close:** "The spec, research and task plan for the remaining milestones are in the repo; the simulation layer is proven, the console is designed, and the voice pipeline is verified against Sarvam's API."

## If asked
- *Why Skydio X2 and not DJI?* The DJI Mini 4 Pro is a live-hardware asset in Cyberwave (not MuJoCo-marked); the first MuJoCo-marked drone we tried crashed the simulator; the X2 flies and renders. Evidence in `docs/RESEARCH.md` §6.
- *Why no live voice?* The Sarvam key reachable from the build session had ₹0 credits; a replacement key only reaches a fresh session. Scripts are ready; cost ≈ ₹0.31 to verify.
- *Costs?* ₹0 hosting (GitHub Pages + Codespaces plan), < 0.1 Cyberwave credits spent on all live tests.
