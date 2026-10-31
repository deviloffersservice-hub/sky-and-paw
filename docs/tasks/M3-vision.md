# M3 — Vision: YOLO detections → fused candidates → waypoints with priority + alerts

**Owner:** vision-engineer · **Branch:** `feat/m3-vision` from `main` (after M2 merges) · **Reviewer:** reviewer · **QA:** qa-engineer (offline on M2 captures + one live run ≤ 0.1 credits)
**Inputs:** SPEC.md §3.2 (real vs synthetic sensors), §4 Victim, §6.3 (fusion weights and triage rules, verbatim), docs/RESEARCH.md §6, M2 frames (`DATA_DIR/missions/<id>/frames/*.jpg` + `mission.json` with pose and intrinsics per frame), `backend/skypaw/scene/site.yaml` (ground-truth victims for the synthetic sensors), Cyberwave waypoint and alert APIs (RESEARCH §1.5, §1.7).

## Goal
`make detect --mission <id>` runs YOLOv8n on every M2 frame, projects boxes to world coordinates, clusters hits into candidates, adds synthetic thermal / UWB radar readings from the scene truth, fuses them with the SPEC formula, suggests triage, writes each candidate as a Cyberwave environment waypoint with metadata, raises a Cyberwave alert per life-sign candidate and per flagged decoy, and emits a `detections.json` the UI and the Go2 mission consume. **Demo:** frames with boxes, a map PNG with candidate pins coloured by suggested triage, the waypoint list in the Cyberwave dashboard, and the alerts list.

## Deliverables
```
backend/skypaw/perception/detector.py   # YOLOv8n (ultralytics pinned) person class only, conf ≥ 0.5, CPU; batch over frames; returns boxes with conf; model weights downloaded once to DATA_DIR/models and hash-checked
backend/skypaw/perception/project.py    # pixel → ground-plane world point using camera pose (MQTT rotation) + intrinsics (fov 58.72°, 640×360) + known altitude (flat footprint); documents the frame conventions (Cyberwave is z-up; mockup/site.yaml is x,z ground, y up: write the converter once, test it)
backend/skypaw/perception/cluster.py    # merge hits within 1.0 m into candidates; keep best conf, mean position, frame refs
backend/skypaw/sensors/synthetic.py     # thermal(candidate|truth) → ΔT with noise; uwb_radar(pos, truth, debris) → {bpm, snr} or negative; acoustic/co2 hooks (used by M4); every output tagged source="synthetic"; deterministic with seed
backend/skypaw/perception/fusion.py     # fused = 1 − Π(1 − wᵢ) with the exact weights from SPEC §6.3; life_sign; suggested triage rules; decoy pattern (thermal-only + radar negative → expectant)
backend/skypaw/perception/publish.py    # waypoints: name "cand-<id>", metadata {victim_id, fused, life_sign, suggested_triage, sensors, frames}; idempotent by name; alerts: severity critical for life sign, warning for decoy/low-confidence, metadata links to the waypoint; dedupe via alert name
scripts/detect.py                       # CLI: --mission <id> --publish --map-png; prints a table of candidates
backend/tests/                          # fusion numbers from SPEC (e.g. rgb .91 + thermal 5.8 + radar strong → ≥ 0.99 and immediate; thermal 9.2 only + radar negative → expectant; rgb .64 only → unknown); projection round-trip; clustering; detector on 5 committed fixture frames from M2 with expected ≥ 1 person box on the frames that show a person placeholder (if the sim renders no humans, document that detection is validated on fixture images of people under tests/fixtures/people/*.jpg and the pipeline on M2 frames tests the plumbing)
tests/fixtures/                         # 5 M2 frames + 5 open-licence person photos (small)
docs/RUNBOOK.md                         # section "Detection"
```

## Known constraint to resolve early
The MuJoCo scene contains no human models; M2 frames show rubble only. M3 must therefore (a) prove YOLO + projection on fixture photos and (b) place **visual victim markers** in the scene for live runs: either a catalog humanoid/mannequin asset that is MuJoCo-marked (check the catalog; record what exists) or a simple coloured box per victim position from site.yaml, detected by a colour heuristic as a stand-in while YOLO stays the production path. Document the choice in RUNBOOK and SPEC §12 via Fable.

## Acceptance criteria
- M3-A1 Fusion unit tests reproduce SPEC §6.3 numbers to 2 decimals; triage suggestions match for V-01 (immediate), V-02 (delayed), V-03 (unknown), D-01 (expectant).
- M3-A2 Projection: a synthetic box at image centre from a drone at (x, 6, z) projects to within 0.3 m of (x, z); off-centre boxes within 0.5 m at 6 m altitude.
- M3-A3 Detector: ≥ 4 of 5 fixture person photos yield a person box ≥ 0.5; mean latency per 640×360 frame ≤ 400 ms on 2 vCPU.
- M3-A4 End-to-end on an M2 mission with victim markers: 3 candidates within 1.0 m of truth positions, decoy flagged expectant, no other candidates; `detections.json` written.
- M3-A5 Publish: 4 waypoints exist in the environment with the metadata schema; second run creates none; ≥ 2 alerts (one critical life-sign, one warning decoy) visible via the alerts API; rerun does not duplicate alerts.
- M3-A6 Map PNG shows pins coloured red/amber/grey matching triage; QA eyeballs against truth.
- M3-A7 Offline tests green; no network in unit tests; weights hash pinned.

## Test plan
Unit as above; live QA: `make detect --mission <M2 id> --publish --map-png` twice; verify waypoints and alerts via SDK; evidence to tests/evidence/M3/REPORT.txt.
