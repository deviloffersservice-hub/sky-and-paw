# Sky & Paw — Product & UI Specification

**Version:** 1.0 · **Status:** approved for build · **Track:** Hardwired (Cyberwave) with Sarvam AI for voice and language
**Interactive reference mockup:** `Sky & Paw Ops v2` (map-first console with phone view). This document describes that mockup as the system to build.

---

## 1. Summary

Sky & Paw is a cloud-run, simulation-only disaster-response system for a single collapsed building. A drone twin maps the site and searches for people, a Unitree Go2 twin reaches survivors to make contact and carry supplies, and a human rescue lead directs a small team from one map-first console. Every teammate carries a phone that shows the lead's words, their own task and route, and the robot cameras in their own language, and lets them talk back and ask for help.

The product has one job: **help one human make good decisions fast while robots and people move through a dangerous site.** The UI is therefore a map with a short queue of decisions, not a dashboard.

### 1.1 Goals
- Map a collapsed site, find people, and fuse several sensors so false positives do not send rescuers on detours.
- Turn spoken commands in Hindi, English or a regional language into structured plans, and never move a robot without the lead's confirmation.
- Propose an efficient allocation of medics, extraction specialists and robots across several survivors, and re-plan when the site changes.
- Keep every person inside the footprint on a known route in and a known route out, and get everyone out on one command.
- Let teammates who speak different languages see, hear and say the same thing.

### 1.2 Non-goals (v1)
- Real hardware. Everything runs in Cyberwave simulation; the architecture is hardware-ready but no robot is driven live.
- Multi-site or multi-building operations.
- Medical decision support beyond START-style triage categories.
- Offline operation of the console.

---

## 2. Actors

| Actor | Device | What they do |
|---|---|---|
| **Rescue lead** (1) | Laptop at the command post | Watches the map and cameras, speaks commands, confirms robot plans, approves allocation proposals and team requests, sets triage, dispatches people by clicking the map, declares evacuation. |
| **Medic** (1–2) | Phone | Reaches a survivor first, stabilises them, reports back by voice. |
| **Extraction specialist** (2, work in pairs) | Phone | Cuts and lifts once the medic has stabilised; reports structural concerns. |
| **Survivor** | None | Hears the Go2 speak in their language; taps or speaks back. |
| **Drone twin** (`dji/mini-4-pro`) | Cyberwave sim | Maps, searches, provides overwatch and light, re-maps on request. |
| **Go2 twin** (`unitree/go2`) | Cyberwave sim | Navigates to survivors or teammates, scans with radar and CO₂, speaks, carries a payload. |

Roles are fixed in v1: `lead`, `medic`, `extraction`. The demo team is one lead, one medic, two extraction specialists, with four languages between them (Hindi, Marathi, Tamil, English).

---

## 3. System context

```
 Lead console (web)  ──────┐                         ┌── Cyberwave Cloud (MuJoCo sim)
   map · cams · voice      │                         │     unitree/go2 twin
                           ├──► Sky & Paw backend ───┤     dji/mini-4-pro twin
 Team phones (web, PWA)    │   (FastAPI + MQTT)      │     GLB scene · waypoints · frames
   feed · map · cam · talk ┘        │                └── MQTT: position / nav status / camera
                                    │
                                    ├──► Sarvam AI
                                    │     saaras (STT) · sarvam-105b (plan) · bulbul (TTS) · sarvam-translate
                                    └──► Perception workers
                                          YOLOv8n on sim frames · synthetic thermal / UWB radar / acoustic / CO₂
```

### 3.1 External services and exact identifiers

| Capability | Service | Identifier / endpoint | Notes |
|---|---|---|---|
| Speech to text (lead and team, live) | Sarvam | `wss://api.sarvam.ai/speech-to-text-realtime/ws`, model `saaras:v3-realtime`, `mode=codemix`, `language_code=auto` | Partials shown live; finals used for planning. |
| Speech to text (clips ≤30 s) | Sarvam | `POST /speech-to-text`, model `saaras:v4`, `keyterms` = victim IDs, team names, sector names | Fallback when the socket is unavailable. |
| Command → plan | Sarvam | `POST /v1/chat/completions`, model `sarvam-105b`, `reasoning_effort=none`, `response_format={type:json_schema, strict:true}` | Guaranteed-valid plan JSON. `sarvam-30b` is deprecated and must not be used. |
| Go2 speech | Sarvam | `POST /text-to-speech`, model `bulbul:v3`, speakers `priya` (hi), `rohan` (mr), `kavitha` (ta), `shubh` (en) | 11 languages. v3 chosen over v4-flash for Malayalam/Odia coverage. |
| Translation of commands, comms and log | Sarvam | `POST /translate`, model `sarvam-translate:v1` | 23 languages. Batch log lines; translate only on change. |
| Twins, sim, waypoints, frames | Cyberwave | Python SDK ≥0.7.4, `CYBERWAVE_API_KEY`; `cw.affect("simulation")`; `twin.navigation.goto / follow_path / wait_for_completion`; `cw.environments.create_waypoint(s)`; `camera.get_frame("numpy")`; `cw.assets.upload_glb` | REST base `https://api.cyberwave.com`. MQTT `mqtt.cyberwave.com:8883` for position, rotation, `navigate/status`, camera. No webhooks: backend subscribes to MQTT and exposes a WebSocket to the browser. |
| Person detection | Own | YOLOv8n on drone and Go2 RGB frames | Pixel → world via depth and camera pose. |

### 3.2 Real vs synthetic sensors
Cyberwave renders **RGB and depth** only. The following are **synthetic sensor models** in v1, implemented in the perception worker against ground-truth victim placements in the scene, and clearly labelled "synthetic" in the UI:
thermal (ΔT vs ambient, with a sun-heated decoy), UWB life-detection radar (bpm, SNR by debris thickness), acoustic (taps after the Go2 speaks), CO₂ (void concentration at the Go2). The fusion, triage and UI treat them exactly as they would treat real sensors, so swapping in hardware changes the worker, not the product.

### 3.3 Positioning of teammates
Pluggable providers, each marker shows its **source and error radius**:
1. **UWB** tags with 3–4 battery anchors around the site — primary, ±0.2 m.
2. **Drone vision** of an AprilTag vest — fallback when UWB drops and the drone can see the person, ±0.5 m.
3. **Phone GPS** outside the footprint, ±4 m.
4. **Stale** — last fix, drawn dashed, when none of the above is available.
In v1 all three are simulated; the demo deliberately drops one teammate's UWB link and shows the switch to vision.

---

## 4. Domain model

| Object | Key fields |
|---|---|
| **Mission** | id, site, collapse time, start time, state (`mapping`, `search`, `reach`, `contact`, `extract`), evacuating flag |
| **Site / grid** | GLB scene, 0.5 m occupancy grid over the footprint and staging: cell class `unmapped`, `passable`, `difficult` (climb), `blocked`; `seen` by drone, `verified` by Go2; staging point |
| **Victim** | id (`V-nn`, decoys become `D-nn` once dismissed), position, where (free text), language, `evidence{rgb, thermal, radar{bpm,snr}, acoustic, co2, human}`, `radarNegative`, fused confidence, `lifeSign`, **suggested triage**, **lead triage** (`immediate` / `delayed` / `unknown` / `expectant`), status (`located` → `medic` → `stabilized` → `extracting` → `extracted`, or `dismissed`), needs (`medic:1, extraction:2`), medic on site, extraction on site [], Go2 state, contact flag, history |
| **Teammate** | id, name, role, language, initials, position, position source + accuracy, `signalLost`, current task (`victim` / `point` / `staging`), route (points, length, climbs, seconds, difficulty, hazards), task queue, work (label, remaining), waiting reason, egress route, body cam on/off |
| **Drone** | position, altitude, mode (`landed`, `takeoff`, `survey`, `hold`, `overwatch_move`, `overwatch`), survey path, light, overwatch target |
| **Go2** | position, heading, mode (`idle`, `moving`, `arrived`, `blocked`), path, target + kind (`victim` / `team` / `point`), payload |
| **Hazard** | id (`H-nn`), position, note, source (lead / team / sim), `collapse` flag (cells blocked) |
| **Route** | ordered points, length m, climbs, estimated seconds (0.7 m/s passable, 0.25 m/s difficult), difficulty (`easy` / `moderate` / `hard`), hazards within 1.8 m |
| **Proposal** | n, reason, ordered victims, tasks per resource (`travel` / `wait` / `work` / `overwatch`, t0, t1, target), warnings, total time, approved flag |
| **Decision** | id, kind, title, body, actions[], linked object; kinds: `plan`, `proposal`, `request`, `cutoff`, `mandown`, `deteriorate`, `newvictim`, `speak`, `warn` |
| **Request** | from teammate, type (`r_go2`, `r_drone`, `r_hazard`, `r_confirm`, `r_dismiss`), note, position, nearest victim, status |
| **Comm** | from, to (null = broadcast), original text and language, translations per team language, evac flag |
| **Log entry** | time, source (`STT`, `LLM`, `DET`, `NAV`, `TTS`, `LEAD`, `TEAM`, `SIM`, `SYS`), key, params, translations |

---

## 5. Screens

### 5.1 Lead console (laptop, ≥1280 px wide; usable to 900 px)

Single-screen application. The map fills everything below a slim header. Nothing else is persistent except the decision queue, the camera tiles and the voice strip. Dark theme only (projector and daylight tent use).

#### 5.1.1 Header
Left to right:
- **Brand** and mode label ("Ops · simulation").
- **Site**: name, mission id, one-line description.
- **Clocks**: mission elapsed `mm:ss`; **time since collapse** `h:mm:ss` in amber.
- **Triage counts**: Immediate (red) · Delayed (amber) · Unknown · Extracted (green). Counts exclude dismissed and extracted from the first three.
- **Team dots**: one circle per teammate, coloured by role (medic green, extraction purple, lead amber square), with a status dot (blue moving, green working) and a dashed red outline when telemetry is lost.
- **EVERYONE OUT**: red button; **hold for 1 s** to trigger. A fill animation shows progress; releasing early cancels.
- **Demo controls** (mockup only): Play/Restart, auto-confirm, speed, Phone view.

#### 5.1.2 Stage: the tactical map
Top-down 2D map, north up, 0.5 m cells, pan by drag, zoom by wheel, scale bar bottom-left, crosshair cursor.

Layers (toggle in **Layers ▾**, all on by default except Egress):
| Layer | Draws |
|---|---|
| Passability | grid cells: unmapped (dark), passable (green), ground-verified (brighter green), difficult/climb (ochre), blocked (red-brown) |
| Thermal | radial heat blobs at thermal detections, radius ∝ ΔT |
| Radar | pulsing rings at radar detections, pulse rate = bpm |
| Team routes | solid route of each moving teammate, coloured by role; dashed when returning to staging |
| Egress routes | dashed blue way-out for everyone inside the footprint (also shown for the selected teammate and during evacuation) |
| Bot paths | drone survey pattern while planned/flying, Go2 path solid while moving, dashed while a Go2 plan awaits confirmation |
| Camera FOV | drone ground footprint (circle from altitude), Go2 forward wedge, body-cam wedges |
| Team | markers with initials, accuracy ring (dashed if stale), label `Name · STATE` (`→ V-01`, `STABILISING`, `WAITING`, `NO SIGNAL`, `STANDBY`, `COMMAND`) and `SOURCE ±m`; labels stack when people stand together |
| People | pins coloured by effective triage (red immediate, amber delayed, grey unknown/expectant, green extracted with tick, grey ✕ dismissed), label `ID` + `TRIAGE` (`?` suffix while the lead has not set it) + `♥` when a life sign exists |
| Hazards | red triangle with id, `COLLAPSE` suffix when cells were blocked, translucent 1.2 m cost zone |

Also drawn: footprint outline (dashed amber), staging box with `STAGING W`, drone (blue cross with rotors, amber dot when light is on, altitude label), Go2 (grey body with amber nose, payload label), hover tooltip for any entity.

**Interactions**
- **Click an entity** (person, teammate, drone, Go2, hazard) → context panel opens on the right; the entity gets a selection ring.
- **Click empty map** → **Pick menu** at the click position (see 5.1.6). A crosshair marks the point.
- **Drag** pans; **wheel** zooms; **⌖** resets view; **Esc** closes menus and panels.
- **3D twin** button swaps the map for the orbitable Three.js twin (rubble, robots, people, routes, hazards as cones, collapse piles). Drag orbits, wheel zooms. Layers are disabled in 3D.

#### 5.1.3 Decision queue (top-right)
A vertical stack of cards, newest first, at most three visible plus "+n more waiting". When empty and the mission is running it shows **NO DECISIONS PENDING**. Each card has a kind label, time, title, one-line body, optional compact timeline (proposals), and 1–4 action buttons. The first action is primary. Cards never time out on their own; acting or dismissing removes them.

| Kind | Trigger | Actions |
|---|---|---|
| Confirm robot plan | Any voice or map command that moves the drone or Go2 | **Confirm · Go**, Cancel |
| Allocation | Mapping complete; victim deteriorates; new candidate accepted; lead presses Recompute | **Approve all**, Review on timeline, Reject |
| Team request | Phone request | **Approve · Go**, Deny |
| Route cut | A teammate's route has no alternative after a collapse or hazard | **Egress now**, Hold position, Send Go2 with supplies |
| Check on teammate | 20 s without telemetry | **Ping phone**, Send Go2 to last fix, Wait |
| Victim weakening | Radar bpm falls below 60 | **Raise to Immediate + replan**, Keep |
| New candidate | Detection after a proposal was approved | **Replan**, Later |
| Go2 speaker | Go2 arrives at a survivor | Phrase buttons (Help is coming / Can you hear me? / Don't move / Medic is close), Not now |
| Role check | Dispatch breaks the role sequence | Understood |

The queue shifts left when the context panel is open so both are visible.

#### 5.1.4 Camera tiles and focus mode (bottom-left)
16:9 tiles: **Drone sees** (RGB + depth, thermal and radar overlays, altitude), **Go2 sees** (front camera, heading), and one **body cam** tile per teammate who has shared their phone camera. Overlays draw YOLO boxes with `ID fused% +ΔT ♥bpm`, "scanning…" while a detection is forming, and `tag XX` on teammates' AprilTag vests in the drone view. The drone tile gets an amber border while it is on overwatch.

**Tap a tile → focus mode:** that feed fills the stage and the map shrinks to a 300×200 minimap bottom-right. Tap again (or ⌖ / Esc) to return.

#### 5.1.5 Voice strip (bottom-centre)
- **Hold** button (push-to-talk). While held, partial transcript streams into the strip with a caret; on release the final sentence is kept with its detected language code.
- Second line: **last message from the team**, translated to English for the lead, with the speaker's name; shows "X speaking…" with live partials while a teammate talks.
- **▾** opens sample commands and a typed-command field (mock microphone in the prototype; in production this is the mic only).
- The Go2's current utterance appears above the strip as a green badge (`Go2 speaking · language · voice`).

#### 5.1.6 Pick menu (on empty-map click)
```
Point (x, z) · near V-02
SEND PEOPLE                 goes immediately
 [PK Priya · Medic] [AS Arun · Extraction] [MN Meera · Extraction]
BOTS                        asks to confirm
 [Go2 · go here · contact · supplies] [Drone · overwatch · light · re-map]
                      [Send selected] [Mark hazard] [Cancel]
```
- Tapping people toggles multi-select; **Send selected** dispatches all of them. If the point is within 1.5 m of a person pin, the dispatch targets that victim (and counts toward its needs); otherwise it is a point task.
- Go2 / Drone create a plan card in the decision queue.
- Mark hazard drops `H-nn` at the point, recomputes routes, logs the source as the lead.
- Teammates without telemetry are shown greyed and cannot be selected.

#### 5.1.7 Context panel (slides in from the right, 372 px)
Opens only on selection; ✕ or Esc closes. Four shapes:

**Survivor**
- `V-01` · status chip · `♥ 64 bpm` chip when a life sign exists.
- Where, position, language.
- **Triage**: four buttons; the system's suggestion has a dashed border and a "suggested" caption; the lead's choice is filled. Changing it logs `LEAD` and feeds the next proposal.
- **Needs**: Medic (needed / name · en route / on site / stabilised), Extraction ×2 (names, "1 of 2"), Go2 (not sent / en route / on site / contact made). Left bar grey → blue active → green done.
- **Send**: one button per teammate with role, plus Go2 (confirm) and Drone overwatch (confirm). People buttons disable when the victim is extracted or dismissed; Go2 disables while it is moving.
- **Evidence** (collapsed by default): one row per sensor with value and note; green bar = present, dimmed = absent/negative. Fused percentage in the summary line.
- Actions: Dismiss (not for extracted), Mark extracted (when medic/stabilised/extracting).

**Teammate**
- Name · role chip; language, position, source ±m, "phone unreachable" when telemetry is lost.
- **State**: what they're doing with ETA or work %, then queued tasks ("then V-02"), and **way out**: egress length, climbs, clear / ⚠ hazards.
- Actions: Return to staging, Go2 to {name}, Drone overwatch, Body cam on/off.
- **Comms**: last four messages to or from this person in English; input to message them (translated to their language on their phone).

**Drone / Go2**
- Mode and target, altitude/battery/light/mapped % (drone) or battery/payload/speed (Go2).
- Drone actions: Light on/off, Re-map building. Go2: **Speaker** with survivor-language selector and the four phrases, a relay field (type → translate → speak), Return to staging.

**Hazard**
- Note, position, source, time; whether cells were blocked; which active routes pass it.

#### 5.1.8 Overlays
- **Plan timeline**: full allocation timeline (rows: each rescuer, Go2, Drone; bars: travel grey, work green, wait amber, overwatch blue; axis in seconds), victim order chips, warnings list, proposal number/reason/state, **Recompute proposal** button.
- **Log & comms**: comms thread (original + translation, evacuation lines in red) and the mission log, both switchable to any team language.
- **Layers**: checkboxes plus a compact legend.

#### 5.1.9 Evacuation bar
When **EVERYONE OUT** fires, a full-width red bar reads "Everyone out · egress routes sent · all robots returning" with a **Stand down** button. All non-warning decisions are cleared, every teammate inside is given a staging task, the Go2 returns to staging, the drone holds, and a broadcast comm is sent in every language.

### 5.2 Team phone (web app, 360 px)
Header: brand, mission id, name · role · language (all chrome in the member's language). Five tabs:

| Tab | Content |
|---|---|
| **Feed** | Red **EVACUATE NOW** card with the spoken evacuation text when active. **Lead says**: live partial while the lead speaks, then the final sentence translated, with original text and language under it and a **🔊 Listen** button (Bulbul in the member's language, on demand). **Your task** card: target and triage, distance, climbs, time, difficulty, hazards on the way, work progress or waiting reason, and **Your way out** length. Then the last eight log lines, translated. |
| **Map** | Square map centred on the member: passability, their own route, their egress route, people, team, hazards, bots. Task card beneath. |
| **Body cam** | The member's camera preview and **Share my camera / Stop sharing**. Sharing adds a tile to the lead's console. |
| **Request** | Five large buttons: Send Go2 to me, Drone overwatch here, Mark hazard here, Confirm victim, Dismiss victim; optional note; "Nearest detection: V-02 · 3.1 m"; list of my requests with Waiting / Approved / Denied. |
| **Talk** | Large hold-to-talk button; live partial transcript; after release, "Sent to lead (translated)" with the Hindi rendering the lead will see; a sample utterance for the demo. |

---

## 6. Behaviour and rules

### 6.1 Command authority
- **People go immediately.** Sending a teammate is a human instruction; the route is computed and pushed to their phone at once and logged as `LEAD`.
- **Robots always confirm.** Any plan that moves the drone or Go2 produces a decision card; nothing moves until **Confirm · Go**. Approving a team request that moves a robot counts as that confirmation (one tap).
- **The lead can override anything**: triage, proposals, dismissals, routes (by re-dispatching).

### 6.2 Voice pipeline (lead)
1. Push-to-talk opens the Sarvam realtime socket (`saaras:v3-realtime`, `codemix`, auto language). Partials render live.
2. On release the final transcript is stored as a comm (broadcast to all phones, translated per language with `sarvam-translate:v1`) and logged as `STT`.
3. The final text goes to `sarvam-105b` with a strict JSON schema covering intents `aerial_survey`, `navigate_go2`, `overwatch`, `speak`, `evacuate`, `broadcast`. Robot intents become plan cards; `evacuate` triggers EVERYONE OUT; `broadcast` is delivered to phones only.
4. Unrecognised commands log `unrecognized` and produce no card.

### 6.3 Mapping and search
- The drone flies five passes at 6 m, 2.5 m apart. Cells within 2.8 m of its ground point become `seen`. The Go2 marks cells within 1.2 m as `verified`.
- Detection fires when the drone passes within 2.6 m of a ground-truth person: RGB (if visible), thermal (if present), radar (positive or **negative**). The Go2 refreshes radar (strong) and CO₂ on arrival.
- Fusion: `fused = 1 − Π(1 − wᵢ)` over present sensors with weights RGB = confidence, thermal = min(1, ΔT/6)·0.7, radar = 0.95 strong / 0.75 weak, acoustic = 0.9, CO₂ = min(1, Δppm/200)·0.5. `lifeSign = radar ∨ acoustic`.
- **Suggested triage**: strong radar → Immediate; weak radar or acoustic only → Delayed; radar negative with no RGB and one sensor → Expectant (decoy pattern); otherwise Unknown. The lead's choice always wins.

### 6.4 Routing
- Dijkstra over the 0.5 m grid, 8-connected. Unmapped and blocked cells are impassable. Difficult cells cost ×3 for people, ×2.2 for the Go2. Cells within 1.2 m of a hazard cost ×4.
- Route metrics: length, climbs (entries into difficult runs), seconds (0.7 m/s passable, 0.25 m/s difficult for people; 0.6/0.3 for the Go2), hazards within 1.8 m, difficulty (hard if any hazard or ≥3 climbs; moderate if ≥1 climb; else easy).
- Every teammate inside the footprint always has an **egress route** to staging, recomputed on every grid or hazard change.

### 6.5 Allocation proposal
Generated automatically when mapping completes, when a victim deteriorates and the lead raises triage, when a new candidate is accepted, and on **Recompute**.
1. Order victims by lead triage (Immediate → Delayed → Unknown; Expectant and dismissed excluded), then by access time.
2. For each victim in order: assign the medic with the earliest arrival (free time + travel); add stabilise work (20 s in the sim; see 9); then the two extraction specialists with earliest arrival; extraction starts at max(medic done, both arrived) and lasts 30 s in the sim. Resources carry their position and free time forward.
3. Go2: travel to the highest-priority victim without contact; Drone: overwatch that victim's route.
4. Warnings: no medic can reach a victim; fewer than two extraction specialists can reach a victim.
5. Output: timeline, order, warnings, total time; shown compactly on the card and fully in the overlay.
**Approve all** assigns the first task to each person, queues the rest in order, sends the Go2 plan, and moves the drone. People already working finish before taking their new task.

### 6.6 Role rules (warn, allow)
On any dispatch to a victim: warn if extraction is heading there with no medic assigned or on site and the victim is not stabilised; warn if a single extraction specialist is assigned and none is on site. Warnings are a `Role check` card and a log line; the dispatch proceeds.

### 6.7 Replanning triggers
| Trigger | System response |
|---|---|
| Hazard pinned | Routes recompute. Moving teammates whose route changes by >1.5 m get the new route and a `rerouted` log line. |
| Secondary collapse | Cells within the radius become blocked and seen; same as hazard, plus `collapse` log. |
| No alternative route | Teammate stops; **Route cut** decision with Egress / Hold / Send Go2. |
| Victim bpm < 60 | **Victim weakening** decision; accepting sets Immediate and proposes a new allocation. |
| New detection after approval | **New candidate** decision; Replan produces a proposal. |
| Teammate silent 20 s | **Check on teammate** decision. |
| Victim dismissed | Everyone tasked to it is released; their queue skips it. |
| Victim extracted | All on-site workers released to their next queued task. |

### 6.8 Requests from phones
Each request becomes a decision card with the member's note and position. Approve executes: Go2 navigation plan to the member with a first-aid kit; drone overwatch above the member (follows them, light on, drone tile pinned); hazard pin at their position; confirm (adds `human` evidence, sets Delayed if unset) or dismiss the nearest detection.

### 6.9 Comms and translation
- Lead broadcast and direct messages are stored once with translations per team language; phones render in the member's language with the original underneath.
- Team talk-back is transcribed in the speaker's language, translated to the lead's language and to the other team languages, shown in the voice strip, the teammate panel and the log.
- Audio on phones is **on demand** (🔊), never auto-played, except nothing is auto-played in v1 at all; evacuation shows the text in red and the lead's voice is relayed by the Go2 speaker where present.

### 6.10 Go2 speaker
Four scripted phrases in Hindi, Marathi, Tamil and English, spoken with `bulbul:v3`; a relay field types or speaks any sentence which is translated to the survivor's language and played. When "Can you hear me?" is spoken on site and the survivor taps, acoustic evidence is added and the victim status becomes **contact**.

---

## 7. User flows

1. **Start and map.** Lead holds the mic: "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो". Partials stream → plan card "Drone: map Block C, search for people" → Confirm → drone takes off, passability fills in, person pins appear with suggested triage. Mapping completes → `Mapping complete: 100% seen, 3 candidates` → **Allocation proposal #1** card.
2. **Triage.** Lead clicks V-02 → panel → sees Delayed (suggested) → leaves it. Clicks D-01 → Expectant suggested, thermal only, radar negative → Dismiss.
3. **Approve allocation.** Lead reads the compact timeline (Priya → V-01 then V-02; Arun + Meera → V-01 after medic; Go2 → V-01; Drone overwatch) → Approve all. Phones update with tasks and routes in each language.
4. **Dispatch by map.** Lead clicks a spot north of V-02, taps Meera, Send selected → Meera's route appears; her phone shows "Go to marked point".
5. **Go2 contact.** Go2 arrives at V-01 → radar 64 bpm strong, CO₂ +120 → card "Go2 is at V-01. Speak?" → Help is coming → Can you hear me? → tapping heard → V-01 contact; Marathi text shown under the Go2 badge.
6. **Collapse and reroute.** Secondary collapse on Priya's path → cells blocked, hazard pinned → Priya rerouted +9 m with a log line; if no path existed, a Route cut card would offer Egress / Hold / Go2.
7. **Team request.** Arun (Tamil) says "இங்கே இருட்டாக இருக்கிறது, ட்ரோன் லைட் வேண்டும்" → lead sees "It's dark here, I need the drone light" → Arun taps Drone overwatch here → card → Approve · Go → drone flies to him, light on, drone tile pinned.
8. **Man-down.** Arun's telemetry stops → after 20 s "Check on teammate" → Ping phone → he responds → marker returns to UWB.
9. **Deterioration replan.** V-02 bpm falls to 59 → "V-02 is weakening" → Raise to Immediate + replan → proposal #2: Priya goes to V-02 right after stabilising V-01; Go2 moves to V-02.
10. **Confirmation from the site.** Priya reaches V-01, says "श्वास आहे पण पाय अडकला आहे", taps Confirm victim → approve → V-01 gains team evidence.
11. **Extraction.** Priya stabilises V-01 (20 s) → Arun and Meera, waiting on site, start extraction → V-01 extracted → both pick up their queued task (V-02).
12. **Everyone out.** Lead holds EVERYONE OUT → red bar, egress routes drawn, phones show EVACUATE NOW in each language, Go2 returns to staging, drone holds; Stand down clears the state.

---

## 8. Feature list

### 8.1 Essential (hackathon deliverable)
- Cyberwave simulation with `unitree/go2` and `dji/mini-4-pro` twins in an uploaded GLB scene; drone survey and Go2 waypoint navigation through the SDK; live position via MQTT → WebSocket.
- YOLOv8n person detection on drone frames; synthetic thermal, radar, acoustic and CO₂; fusion and suggested triage; one decoy that the system flags and the lead dismisses.
- Occupancy grid with fog of war, route planner with climbs/hazard costs, egress routes.
- Lead console as specified: map with layers, decision queue, camera tiles with focus mode, voice strip, context panel, pick menu, plan timeline overlay, log & comms overlay, 3D twin toggle, EVERYONE OUT.
- Sarvam realtime STT for the lead, `sarvam-105b` structured plans, `bulbul:v3` Go2 speech in four languages, `sarvam-translate:v1` for comms and log.
- Phone app: Feed, Map, Request, Talk tabs; translated lead speech; task and egress card; requests with approval; talk-back.
- Allocation proposal with timeline; approve/queue; replanning on collapse, deterioration, new candidate; role warnings; man-down check.
- Positioning providers simulated with source and accuracy shown; UWB-drop → vision fallback demo.

### 8.2 Good to have
- Body cam sharing from phones (present in the mockup; real WebRTC in production).
- On-demand read-aloud on phones (🔊).
- Drag a teammate between rows on the plan timeline to edit a proposal before approving.
- Per-victim notes and photos from the site.
- Second decoy types (mannequin: RGB only + cold).
- Re-map a region on demand from the context panel.
- Replay of the mission from the log.

### 8.3 Future
- Real UWB anchors and AprilTag vests; real Go2 and drone through `cw.affect("live")`.
- WiFi-sensing respiration detection as an additional sensor.
- Structural-engineer and K9-handler roles; configurable roles per mission.
- Multi-building sites and hand-off between leads.
- Offline-tolerant phones with store-and-forward.

---

## 9. Simulation parameters and real-world equivalents

| Parameter | Sim value (demo pacing) | Real-world value to state in pitch |
|---|---|---|
| Stabilise a survivor | 20 s | 5–15 min |
| Extraction (pair) | 30 s | 20–90 min |
| Walking speed passable / difficult | 0.7 / 0.25 m/s | similar |
| Go2 speed | 0.6 m/s | 0.5–1.0 m/s |
| Drone survey speed / altitude / spacing | 2.2 m/s / 6 m / 2.5 m | similar |
| Telemetry loss → man-down check | 20 s | 60–120 s |
| Radar weakening threshold | <60 bpm, falling | per medical protocol |
| Cyberwave cost | 0.01 credits/min MuJoCo; 50 free credits/month | — |
| Sarvam budget | ₹100 free ≈ 200 min STT or 33k TTS chars | — |

Latency budget on stage: STT partials <300 ms, final + plan card <2.5 s, TTS first audio <1.5 s, translation per line <800 ms.

---

## 10. Edge cases

| Situation | Handling |
|---|---|
| Collapse cuts a teammate's only route | Route cut decision; egress offered; drone re-map suggested when no route at all. |
| Teammate stops moving / phone offline | 20 s → Check on teammate; marker dashed; last fix kept; ping / Go2 relay. |
| More victims than rescuers | Proposal orders by triage then access; lead sees total time and warnings; Expectant is only ever set by the lead. |
| Victim deteriorates | Decision to raise triage and replan; otherwise nothing changes silently. |
| New victim after approval | Decision to replan; current tasks continue until approved. |
| Detection turns out false after dispatch | Dismiss releases assigned people and skips it in queues. |
| Two rescuers assigned to a narrow route | Not modelled in v1; timeline shows overlapping travel so the lead can stagger. |
| Go2 cannot reach target after collapse | Go2 marked blocked; lead re-sends or recalls. |
| Drone or Go2 battery low | Shown in bot panel; no automation in v1. |
| Lead's command mis-transcribed | Final text is shown before any plan; plan card restates the intent; Cancel costs one tap. |
| Lead is overwhelmed | Only decision kinds listed in 5.1.3 interrupt; everything else is log. |
| Aftershock | EVERYONE OUT: one gesture, every phone, every language, robots recalled. |
| Lost UWB | Vision fallback with ±0.5 m, else stale dashed marker with last fix. |

---

## 11. Acceptance criteria

**Map and selection**
- A1. Given a running mission, when the lead clicks a person pin, then the context panel opens within 200 ms showing id, status, triage bar, needs, send buttons and collapsed evidence.
- A2. Given the lead clicks empty map, then a pick menu appears at the click with all non-lead teammates, Go2, Drone, Mark hazard and Cancel; teammates without telemetry are disabled.
- A3. Given two teammates are selected in the pick menu and Send selected is tapped, then both receive a route immediately, the routes are drawn in their role colour, and `LEAD` dispatch lines are logged for each.
- A4. Given a point within 1.5 m of a victim is chosen, then the dispatch targets that victim and its Needs block updates.
- A5. Given the Egress layer is off, when a teammate inside the footprint is selected, then their egress route is drawn dashed blue.

**Robots**
- B1. Given a voice command that moves a robot, then no robot moves until Confirm · Go is tapped; Cancel logs and discards the plan.
- B2. Given the drone survey completes, then ≥95% of footprint cells are seen and a proposal card appears within 1 s.
- B3. Given the Go2 arrives at a victim, then radar and CO₂ evidence update within 2 s (sim) and a Go2 speaker card appears.
- B4. Given a phrase is spoken while the Go2 is on site, then the log records the exact text and language and the badge shows voice and language.

**Fusion and triage**
- C1. Given a thermal-only detection with negative radar and no RGB, then the suggested triage is Expectant and the pin is grey with `?`.
- C2. Given RGB-only detection, then the suggestion is Unknown, never Expectant.
- C3. Given strong radar, then the suggestion is Immediate and the pin shows `♥`.
- C4. Given the lead sets a triage, then counts in the header, pin colour and the next proposal order reflect it, and the dashed suggestion remains visible.

**Allocation**
- D1. Given three live victims and one medic, then the proposal sends the medic to them in triage order and extraction work never starts before stabilisation in the timeline.
- D2. Given fewer than two extraction specialists can reach a victim, then the proposal carries a warning and the card shows ⚠.
- D3. Given Approve all, then each teammate starts their first task, queues the rest, and the Go2 plan starts without a further confirmation.
- D4. Given a victim's bpm drops below 60, then a Victim weakening card appears once; accepting sets Immediate and creates proposal n+1.

**Replanning and safety**
- E1. Given a collapse on a moving teammate's route with an alternative, then their route updates within one frame and a `rerouted` log line states the extra metres.
- E2. Given a collapse with no alternative, then the teammate stops and a Route cut card offers Egress now, Hold position, Send Go2 with supplies.
- E3. Given a teammate silent for 20 s, then one Check on teammate card appears with Ping, Send Go2, Wait.
- E4. Given extraction is dispatched to a victim with no medic assigned, then a Role check card and a `role_warn` log line appear and the dispatch still proceeds.
- E5. Given EVERYONE OUT is held for 1 s, then every non-lead teammate gets a staging task, the Go2 heads to staging, the red bar appears, all phones show EVACUATE NOW in their language, and non-warning decisions are cleared. Releasing before 1 s does nothing.

**Comms and phones**
- F1. Given the lead speaks Hindi, then each phone shows the sentence in its member's language with the original beneath, and partials appear while the lead is still speaking.
- F2. Given a teammate talks back in Tamil, then the voice strip shows it in English for the lead within 2.5 s of release and the log stores all four translations.
- F3. Given a phone request, then a card appears for the lead; Approve executes the matching action and the phone shows Approved; Deny shows Denied.
- F4. Given a teammate shares their camera, then a body-cam tile appears on the console with a FOV wedge on the map; stopping removes both.

**Cameras**
- G1. Given a camera tile is tapped, then it fills the stage and the map becomes a 300×200 minimap; tapping again restores the layout.
- G2. Given a person is within a camera's view, then a box with id, fused % and available ΔT / bpm is drawn over the feed.

**Quality**
- H1. The console renders at 1280 px and 1500 px without horizontal scroll; the phone view renders at 360 px without horizontal scroll.
- H2. No uncaught JavaScript errors during the full automated demo.
- H3. All interactive controls have a visible focus state; Esc closes any open menu, panel or overlay.

---

## 12. Assumptions and open items
- Obstacle-aware navigation inside Cyberwave's MuJoCo for the Go2 is **unverified**; v1 plans its own path on the grid and sends dense waypoints with `follow_path`. Verify on day one.
- Collision geometry for an arbitrary rubble GLB in MuJoCo is **unverified**; fall back to a URDF with STL collision meshes if needed.
- Cyberwave offers no webhooks; the backend bridges MQTT to the browser.
- Realtime STT WebSocket beta access on Sarvam is **unverified**; the REST fallback with `saaras:v4` is required.
- Teammate positions, thermal, radar, acoustic and CO₂ are simulated in v1 and labelled as such in the UI and the pitch.

## 13. Glossary
**Footprint** — the 18 × 12 m building outline. **Staging** — the safe assembly point west of the footprint. **Seen / verified** — grid cell observed by the drone / walked by the Go2. **Fused** — combined confidence across sensors. **Life sign** — radar heartbeat or acoustic response. **START triage** — Immediate / Delayed / Minor / Expectant field triage; v1 uses Immediate / Delayed / Unknown / Expectant. **Egress** — a person's current route out to staging. **Decision** — any item that requires the lead's choice. **Proposal** — an allocation of people and robots to victims over time.
