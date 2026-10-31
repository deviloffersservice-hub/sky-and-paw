#!/usr/bin/env node
/**
 * Mock Sky & Paw backend implementing docs/PROTOCOL.md for console development and tests.
 *
 *   npm run mock                      # http://127.0.0.1:7861, ws://127.0.0.1:7861/ws
 *   node tests/mock-server.mjs --port 7861 --static dist --speed 4
 *
 * Open the console with ?api=http://127.0.0.1:7861 (or serve dist from here with --static).
 *
 * Scripted mission (mission seconds at --speed 1): sim starting → ready (2 s) → drone takes
 * off and flies the survey lawnmower, revealing the grid → V-01..V-03 appear as the drone
 * passes → survey done → plan decision "Go2 → V-01" (waits for confirm/cancel) → Go2 walks
 * its path → arrives → Go2 speaker card + Bulbul TTS playing.
 * Client messages are applied (triage, dismiss, command, tts, text_command, mic + binary PCM).
 *
 * Test hooks: GET /__mock/received (every client message; binary as {binary: bytes}),
 * POST /__mock/reset (restart the script, clear received), POST /__mock/advance?s=N.
 */
import { createServer } from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WebSocketServer } from "ws";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const FRAMES_DIR = path.join(ROOT, "tests", "evidence", "M2", "dev");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--")) return process.argv[i + 1];
  return fallback;
}
const PORT = Number(arg("port", process.env.MOCK_PORT || 7861));
const HOST = arg("host", process.env.MOCK_HOST || "127.0.0.1");
const SPEED = Number(arg("speed", process.env.MOCK_SPEED || 1));
const STATIC = arg("static", process.env.MOCK_STATIC || "");
const STATIC_DIR = STATIC ? path.resolve(process.cwd(), STATIC) : "";

/* ---------- site and grid (world frame: x right, y up, z up) ---------- */
// Rubble boxes from the mockup in its site frame: [x, z, w, h, d, rot].
const RUBBLE = [
  [-5, -3, 6, 0.35, 4, 0.2], [1, -3.5, 5, 0.3, 3.5, -0.15], [-2, 2.5, 5, 0.3, 4, 0.1], [-6.5, 3.5, 3.5, 0.3, 3, 0.35], [7, -4, 3, 0.3, 3, 0.05], [7.5, 3.5, 2.5, 0.3, 4, -0.3], [0.5, 1, 3, 0.3, 2.5, 0.6],
  [-9, 0, 0.3, 2.8, 6, 0], [-3, -6, 5, 2.2, 0.3, 0], [9, -2, 0.3, 1.8, 5, 0],
  [-7, -1, 1.2, 1, 1.4, 0.4], [-3.5, -0.5, 1, 0.8, 1, 0.9], [2, -5, 1.5, 0.9, 1.2, 0.3], [-1, 4.5, 1.1, 1.2, 1, 0.7], [6, 1.5, 1, 0.7, 1.3, 0.2], [-5, 5, 0.9, 0.6, 0.9, 0.1], [1.5, 4.2, 1.3, 0.8, 0.9, -0.5], [-8, -4.5, 1.2, 1.5, 1, 0.3],
];
const G = { x0: -13, y0: -9, cs: 0.5, w: 52, h: 36 };
const N = G.w * G.h;
const truth = new Uint8Array(N);
const center = (i, j) => [G.x0 + (i + 0.5) * G.cs, G.y0 + (j + 0.5) * G.cs];
(function buildTruth() {
  for (let j = 0; j < G.h; j++) {
    for (let i = 0; i < G.w; i++) {
      const [x, y] = center(i, j);
      const z = -y; // world y = -site z
      let cls = 1;
      for (const r of RUBBLE) {
        const dx = x - r[0], dz = z - r[1], c = Math.cos(-r[5]), s = Math.sin(-r[5]);
        const lx = dx * c - dz * s, lz = dx * s + dz * c;
        if (Math.abs(lx) <= r[2] / 2 && Math.abs(lz) <= r[4] / 2) cls = Math.max(cls, r[3] >= 0.6 ? 3 : 2);
      }
      truth[j * G.w + i] = cls;
    }
  }
  const copy = truth.slice();
  for (let j = 1; j < G.h - 1; j++) for (let i = 1; i < G.w - 1; i++) {
    if (copy[j * G.w + i] !== 1) continue;
    let nb = false;
    for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) if (copy[(j + b) * G.w + i + a] === 3) nb = true;
    if (nb) truth[j * G.w + i] = 2;
  }
})();
const CLS_B64 = Buffer.from(truth).toString("base64");

function packbits(mask) {
  const out = Buffer.alloc(Math.ceil(mask.length / 8));
  for (let k = 0; k < mask.length; k++) if (mask[k]) out[k >> 3] |= 0x80 >> (k & 7);
  return out.toString("base64");
}

const VICTIMS = [
  { id: "V-01", pos: [3.5, 2.0, 0.5], evidence: { rgb: 0.91, thermal: 5.8, radar: { bpm: 64, snr: "strong" }, acoustic: null, co2: null }, fused: 0.93, life_sign: true, suggested_triage: "immediate", source: { rgb: "real", thermal: "synthetic", radar: "synthetic" } },
  { id: "V-02", pos: [-4.0, -3.0, 0.6], evidence: { rgb: null, thermal: 3.1, radar: { bpm: 71, snr: "weak" }, acoustic: null, co2: null }, fused: 0.78, life_sign: true, suggested_triage: "delayed", source: { thermal: "synthetic", radar: "synthetic" } },
  { id: "V-03", pos: [7.0, -4.8, 0.5], evidence: { rgb: 0.64, thermal: null, radar: null, acoustic: null, co2: null }, fused: 0.64, life_sign: false, suggested_triage: "unknown", source: { rgb: "real" } },
];
const SURVEY_WPS = [5, 2.5, 0, -2.5, -5].flatMap((y, k) => (k % 2 ? [[8, y, 6], [-8, y, 6]] : [[-8, y, 6], [8, y, 6]]));
SURVEY_WPS.push([-11.5, -5.5, 6]);
const DRONE_HOME = [-11.5, -5.5, 0.2];
const GO2_HOME = [-11, -7.5, 0];
const PHRASES = {
  p_help: { "en-IN": "Help is on the way. Stay calm.", "hi-IN": "मदद आ रही है। शांत रहें।", "mr-IN": "मदत येत आहे. शांत राहा.", "ta-IN": "உதவி வருகிறது. அமைதியாக இருங்கள்." },
  p_hear: { "en-IN": "Can you hear me? Tap twice on something.", "hi-IN": "क्या आप मुझे सुन सकते हैं? दो बार थपथपाएँ।", "mr-IN": "तुम्हाला ऐकू येतंय का? दोनदा टकटक करा.", "ta-IN": "என் குரல் கேட்கிறதா? இரண்டு முறை தட்டுங்கள்." },
  p_still: { "en-IN": "Don't move. Cover your mouth and nose.", "hi-IN": "हिलें नहीं। मुँह और नाक ढक लें।", "mr-IN": "हलू नका. तोंड आणि नाक झाका.", "ta-IN": "அசையாதீர்கள். வாய், மூக்கை மூடுங்கள்." },
  p_medic: { "en-IN": "A medic is two minutes away. Keep breathing slowly.", "hi-IN": "मेडिक दो मिनट दूर है। धीरे-धीरे साँस लेते रहें।", "mr-IN": "मेडिक दोन मिनिटांवर आहे. हळू श्वास घ्या.", "ta-IN": "மருத்துவர் இரண்டு நிமிடத்தில். மெதுவாக சுவாசியுங்கள்." },
};
const VOICES = { "hi-IN": "priya", "mr-IN": "rohan", "ta-IN": "kavitha", "en-IN": "shubh" };
const MIC_SENTENCE = "dog को V-02 के पास भेजो";

const L4 = (en, hi, mr, ta) => ({ "en-IN": en, "hi-IN": hi ?? en, "mr-IN": mr ?? en, "ta-IN": ta ?? en });

/* ---------- mission state ---------- */
let M;
let received = [];
let seq = 0;
function reset() {
  seq = 0;
  M = {
    t: 0,
    phase: "idle",
    sim: { status: "starting", ready: false, env_id: "mock-env", requested_s: 300, elapsed_s: 0, credits_est: 0, error: null },
    drone: { pos: null, yaw: 0, mode: "landed", battery: 84, pose_age_s: null, frame_id: null, wp: 0 },
    go2: { pos: null, yaw: Math.PI / 2, mode: "idle", battery: 71, pose_age_s: null, target: null, frame_id: null, path: [] },
    seen: new Uint8Array(N),
    victims: [],
    decisions: [],
    transcript: { active: false, partial: "", final: "", lang: null, final_t: null },
    tts: { playing: null },
    ttsUntil: 0,
    log: [],
    frames: 0,
    lastFrameWall: 0,
    surveyDone: false,
    micFrames: 0,
    micLang: "auto",
  };
  for (let j = 0; j < G.h; j++) for (let i = 0; i < G.w; i++) {
    const [x, y] = center(i, j);
    if (Math.abs(x) > 9.8 || Math.abs(y) > 6.8) M.seen[j * G.w + i] = 1;
  }
  log("SYS", "mission_start", L4("Mission SP-0417 started. Simulation starting (Cyberwave MuJoCo).", "मिशन SP-0417 शुरू। सिमुलेशन शुरू हो रहा है।", "मोहीम SP-0417 सुरू. सिम्युलेशन सुरू होत आहे.", "பணி SP-0417 தொடங்கியது."));
}

function log(src, key, text) {
  M.log.push({ t: +M.t.toFixed(2), src, key, text });
  if (M.log.length > 50) M.log.shift();
}

function decision(d) {
  const id = `D-${++seq}`;
  const dec = { id, created_t: +M.t.toFixed(2), body: "", ...d };
  M.decisions.unshift(dec);
  return dec;
}

function planDecision(intent, target, sourceText, sourceLang) {
  const pid = `P-${seq + 1}`;
  const titles = {
    aerial_survey: "Drone: map Block C, search for people",
    navigate_go2: `Go2 → ${target ?? "nearest life sign"}`,
    speak: "Go2 speaks to the survivor",
    land: "Drone: land at staging",
  };
  const steps = {
    aerial_survey: ["Take off to 6 m", "Lawnmower over 18 × 12 m", "Detect people", "Return"],
    navigate_go2: ["Plan route on the passability grid", `Walk to ${target ?? "nearest"} at 0.6 m/s`, "Radar + CO₂ scan on arrival"],
    speak: ["bulbul:v3 · rohan", "Play at the Go2 speaker"],
    land: ["Descend", "Land at staging W"],
  };
  const d = decision({
    kind: "plan",
    title: titles[intent] ?? intent,
    body: (steps[intent] ?? []).join(" · "),
    plan: { id: pid, intent, target, steps: steps[intent] ?? [], risk: "low", est_s: intent === "navigate_go2" ? 48 : 30, source_text: sourceText ?? null, source_lang: sourceLang ?? null, model: "sarvam-105b" },
    actions: [{ id: "go", label: "Confirm · Go", primary: true }, { id: "cancel", label: "Cancel" }],
  });
  log("LLM", "plan_ready", L4(`Plan ready: ${d.title}`, `योजना तैयार: ${d.title}`, `योजना तयार: ${d.title}`, `திட்டம் தயார்: ${d.title}`));
  return d;
}

function go2PathTo(target) {
  const from = M.go2.pos ?? GO2_HOME;
  const mid1 = [-9.5, from[1], 0];
  const mid2 = [-9.5, target[1] - 0.8, 0];
  const end = [target[0] - 0.8, target[1] - 0.8, 0];
  const pts = [mid1, mid2, [end[0] * 0.5 + mid2[0] * 0.5, end[1], 0], end];
  return pts.map((p) => p.map((v) => +v.toFixed(2)));
}

function startTts(text, lang) {
  const sha = Buffer.from(`${lang}:${text}`).toString("hex").slice(0, 16);
  M.tts.playing = { text, lang, voice: VOICES[lang] ?? "rohan", url: `/tts/${sha}.wav`, started_t: +M.t.toFixed(2) };
  M.ttsUntil = Date.now() + 5000;
  log("TTS", "tts", L4(`Go2 spoke (${lang}): “${text}”`, `Go2 ने कहा (${lang}): “${text}”`, `Go2 म्हणाला (${lang}): “${text}”`, `Go2 பேசியது (${lang}): “${text}”`));
}

function execPlan(d) {
  const p = d.plan;
  M.decisions = M.decisions.filter((x) => x.id !== d.id);
  log("LEAD", "plan_confirmed", L4(`Lead approved: ${d.title}`, `लीड ने मंज़ूर किया: ${d.title}`, `लीडने मंजूर केले: ${d.title}`, `தலைவர் ஒப்புதல்: ${d.title}`));
  if (p.intent === "aerial_survey") {
    M.drone.mode = "takeoff";
    M.drone.wp = 0;
    M.surveyDone = false;
  } else if (p.intent === "navigate_go2") {
    const v = M.victims.find((x) => x.id === p.target) ?? M.victims.find((x) => x.life_sign);
    if (!v) return;
    M.go2.target = v.id;
    M.go2.path = go2PathTo(v.pos);
    M.go2.mode = "moving";
    v.status = "enroute";
    M.phase = "reach";
    log("NAV", "nav_start", L4(`Go2 → ${v.id}: path ${M.go2.path.length} points.`));
  } else if (p.intent === "land") {
    M.drone.mode = "landing";
  } else if (p.intent === "speak") {
    startTts(PHRASES.p_medic["mr-IN"], "mr-IN");
  }
}

function applyMessage(msg) {
  switch (msg.type) {
    case "confirm": {
      const d = M.decisions.find((x) => x.plan && x.plan.id === msg.plan_id);
      if (d) execPlan(d);
      return { accepted: !!d };
    }
    case "cancel": {
      const d = M.decisions.find((x) => x.plan && x.plan.id === msg.plan_id);
      if (d) {
        M.decisions = M.decisions.filter((x) => x !== d);
        log("LEAD", "plan_cancelled", L4(`Lead cancelled: ${d.title}`, `लीड ने रद्द किया: ${d.title}`, `लीडने रद्द केले: ${d.title}`, `தலைவர் ரத்து: ${d.title}`));
      }
      return { accepted: !!d };
    }
    case "decide": {
      const d = M.decisions.find((x) => x.id === msg.decision_id);
      if (!d) return { accepted: false, reason: "unknown decision" };
      M.decisions = M.decisions.filter((x) => x !== d);
      if (d.kind === "speak" && PHRASES[msg.action]) startTts(PHRASES[msg.action]["mr-IN"], "mr-IN");
      return { accepted: true };
    }
    case "command": {
      if (msg.intent === "stop_all") {
        if (M.drone.mode !== "landed") M.drone.mode = "hold";
        M.go2.mode = "idle";
        M.go2.path = [];
        log("LEAD", "stop_all", L4("Stop all: drone hold, Go2 stop."));
        return { accepted: true };
      }
      planDecision(msg.intent, msg.target ?? null, null, null);
      return { accepted: true };
    }
    case "triage": {
      const v = M.victims.find((x) => x.id === msg.victim_id);
      if (!v) return { accepted: false };
      v.triage = msg.category;
      log("LEAD", "triage_set", L4(`${v.id} triage set to ${msg.category} by lead.`, `${v.id} ट्राइएज ${msg.category} — लीड।`, `${v.id} ट्रायएज ${msg.category} — लीड.`, `${v.id} முன்னுரிமை ${msg.category} — தலைவர்.`));
      return { accepted: true };
    }
    case "dismiss": {
      const v = M.victims.find((x) => x.id === msg.victim_id);
      if (v) v.status = "dismissed";
      return { accepted: !!v };
    }
    case "tts": {
      const lang = msg.lang || "mr-IN";
      const text = msg.text || (PHRASES[msg.phrase_id] ?? PHRASES.p_help)[lang] || PHRASES.p_help["en-IN"];
      startTts(text, lang);
      return { accepted: true };
    }
    case "text_command": {
      finalTranscript(msg.text, msg.lang || "hi-IN");
      return { accepted: true };
    }
    case "mic": {
      if (msg.state === "start") {
        M.transcript = { active: true, partial: "", final: "", lang: msg.lang, final_t: null };
        M.micFrames = 0;
        M.micLang = msg.lang;
      } else if (msg.state === "stop" && M.transcript.active) {
        finalTranscript(MIC_SENTENCE, "hi-IN");
      }
      return { accepted: true };
    }
    default:
      return { accepted: false, reason: `unknown type ${msg.type}` };
  }
}

function finalTranscript(text, lang) {
  M.transcript = { active: false, partial: "", final: text, lang, final_t: +M.t.toFixed(2) };
  log("STT", "stt_final", L4(`Lead (${lang}): “${text}”`, `लीड (${lang}): “${text}”`, `लीड (${lang}): “${text}”`, `தலைவர் (${lang}): “${text}”`));
  const t = text.toLowerCase();
  const m = t.match(/v-?0?([123])/);
  if (/(drone|ड्रोन|survey|map|मैप)/.test(t)) planDecision("aerial_survey", null, text, lang);
  else if (/(dog|go2|भेज|send)/.test(t)) planDecision("navigate_go2", m ? `V-0${m[1]}` : null, text, lang);
  else if (/(tell|say|बोल|कह)/.test(t)) planDecision("speak", null, text, lang);
  else log("LLM", "unrecognized", L4("Command not understood.", "आदेश समझ नहीं आया।", "आदेश समजला नाही.", "கட்டளை புரியவில்லை."));
}

function onBinary(buf) {
  if (!M.transcript.active) return;
  M.micFrames++;
  const words = MIC_SENTENCE.split(" ");
  M.transcript.partial = words.slice(0, Math.min(words.length, Math.ceil(M.micFrames / 3))).join(" ");
}

/* ---------- simulation tick ---------- */
function reveal(x, y, r) {
  const n = Math.ceil(r / G.cs);
  const ci = Math.floor((x - G.x0) / G.cs), cj = Math.floor((y - G.y0) / G.cs);
  for (let a = -n; a <= n; a++) for (let b = -n; b <= n; b++) {
    const i = ci + a, j = cj + b;
    if (i < 0 || j < 0 || i >= G.w || j >= G.h) continue;
    const [px, py] = center(i, j);
    if (Math.hypot(px - x, py - y) <= r) M.seen[j * G.w + i] = 1;
  }
}

function pctMapped() {
  let seen = 0, tot = 0;
  for (let j = 0; j < G.h; j++) for (let i = 0; i < G.w; i++) {
    const [x, y] = center(i, j);
    if (Math.abs(x) <= 9 && Math.abs(y) <= 6) { tot++; if (M.seen[j * G.w + i]) seen++; }
  }
  return Math.round((seen / tot) * 100);
}

function moveToward(pos, target, step) {
  const dx = target[0] - pos[0], dy = target[1] - pos[1], dz = (target[2] ?? pos[2]) - pos[2];
  const d = Math.hypot(dx, dy, dz);
  if (d <= step) return { pos: [target[0], target[1], target[2] ?? pos[2]], arrived: true, yaw: d > 1e-6 ? Math.atan2(dy, dx) : null };
  return { pos: [pos[0] + (dx / d) * step, pos[1] + (dy / d) * step, pos[2] + (dz / d) * step], arrived: false, yaw: Math.hypot(dx, dy) > 1e-3 ? Math.atan2(dy, dx) : null };
}

function tick(dt) {
  M.t += dt;
  M.sim.elapsed_s = +M.t.toFixed(1);
  M.sim.credits_est = +((M.t / 60) * 0.01).toFixed(3);
  if (M.t >= 1 && !M.drone.pos) {
    M.drone.pos = [...DRONE_HOME];
    M.go2.pos = [...GO2_HOME];
  }
  if (M.t >= 2 && !M.sim.ready) {
    M.sim.status = "ready";
    M.sim.ready = true;
    log("SIM", "sim_ready", L4("Simulation ready.", "सिमुलेशन तैयार।", "सिम्युलेशन तयार.", "சிமுலேஷன் தயார்."));
  }
  if (M.drone.pos) { M.drone.pose_age_s = 0.1; M.go2.pose_age_s = 0.1; }
  // survey starts by itself at t = 3 (the scripted mission)
  if (M.t >= 3 && M.phase === "idle") {
    M.phase = "mapping";
    M.drone.mode = "takeoff";
    log("SYS", "drone_takeoff", L4("Drone airborne at 6 m. Mapping.", "ड्रोन 6 मी. पर। मैपिंग।", "ड्रोन 6 मी. वर. मॅपिंग.", "ட்ரோன் 6 மீ. வரைபடம்."));
  }
  const d = M.drone;
  if (d.mode === "takeoff") {
    d.pos[2] = Math.min(6, d.pos[2] + 3 * dt);
    if (d.pos[2] >= 6) d.mode = "survey";
  } else if (d.mode === "survey") {
    const wp = SURVEY_WPS[d.wp];
    const r = moveToward(d.pos, wp, 4 * dt);
    d.pos = r.pos;
    if (r.yaw != null) d.yaw = r.yaw;
    if (r.arrived) d.wp++;
    reveal(d.pos[0], d.pos[1], 6 * Math.tan((36 * Math.PI) / 180));
    for (const v of VICTIMS) {
      if (!M.victims.some((x) => x.id === v.id) && Math.hypot(v.pos[0] - d.pos[0], v.pos[1] - d.pos[1]) < 4) {
        M.victims.push({ ...structuredClone(v), status: "located", triage: null, frames: [`f_d${String(M.frames).padStart(6, "0")}`] });
        M.phase = "search";
        log("DET", "detection", L4(`${v.id}: detected at (${v.pos[0]}, ${v.pos[1]}) · fused ${Math.round(v.fused * 100)}%`));
      }
    }
    if (d.wp >= SURVEY_WPS.length) {
      d.mode = "hold";
      d.wp = SURVEY_WPS.length - 1;
      if (!M.surveyDone) {
        M.surveyDone = true;
        log("SYS", "survey_done", L4(`Mapping complete: ${pctMapped()}% seen, ${M.victims.length} candidates.`));
        if (!M.decisions.some((x) => x.plan && x.plan.intent === "navigate_go2")) planDecision("navigate_go2", "V-01", "dog को V-01 के पास भेजो", "hi-IN");
      }
    }
  } else if (d.mode === "landing") {
    d.pos[2] = Math.max(0.2, d.pos[2] - 2 * dt);
    if (d.pos[2] <= 0.2) d.mode = "landed";
  }
  const g = M.go2;
  if (g.mode === "moving" && g.path.length) {
    const r = moveToward(g.pos, g.path[0], 1.2 * dt);
    g.pos = r.pos;
    if (r.yaw != null) g.yaw = r.yaw;
    if (r.arrived) g.path.shift();
    if (!g.path.length) {
      g.mode = "arrived";
      const v = M.victims.find((x) => x.id === g.target);
      if (v) {
        v.status = "arrived";
        v.evidence.co2 = 420;
        M.phase = "contact";
        log("NAV", "nav_arrived", L4(`Go2 at ${v.id}. Radar + CO₂ scan.`, `Go2 ${v.id} पर। रडार + CO₂ स्कैन।`, `Go2 ${v.id} वर. रडार + CO₂ स्कॅन.`, `Go2 ${v.id} இல். ரேடார் + CO₂.`));
        decision({
          kind: "speak",
          title: `Go2 at ${v.id} · speak to the survivor`,
          body: "Bulbul in Marathi at the Go2 speaker",
          actions: [{ id: "p_help", label: "Help is coming", primary: true }, { id: "p_hear", label: "Can you hear me?" }, { id: "p_still", label: "Don't move" }, { id: "p_medic", label: "Medic is close" }, { id: "not_now", label: "Not now" }],
        });
        startTts(PHRASES.p_help["mr-IN"], "mr-IN");
      }
    }
  }
  if (M.tts.playing && Date.now() > M.ttsUntil) M.tts.playing = null;
  // camera frames once per wall-clock second while the sim is ready
  if (M.sim.ready && Date.now() - M.lastFrameWall >= 1000) {
    M.lastFrameWall = Date.now();
    M.frames++;
    d.frame_id = `f_d${String(M.frames).padStart(6, "0")}`;
    g.frame_id = `f_g${String(M.frames).padStart(6, "0")}`;
  }
}

function snapshot() {
  const r3 = (p) => (p ? p.map((v) => +v.toFixed(3)) : null);
  return {
    type: "snapshot",
    t: +M.t.toFixed(2),
    ts: new Date().toISOString(),
    mission: { id: "SP-0417", phase: M.phase, since_collapse_s: 2520 + Math.floor(M.t) },
    sim: { ...M.sim },
    robots: {
      drone: { pos: r3(M.drone.pos), yaw: +M.drone.yaw.toFixed(3), mode: M.drone.mode, battery: M.drone.battery, pose_age_s: M.drone.pose_age_s, frame_id: M.drone.frame_id },
      go2: { pos: r3(M.go2.pos), yaw: +M.go2.yaw.toFixed(3), mode: M.go2.mode, battery: M.go2.battery, pose_age_s: M.go2.pose_age_s, target: M.go2.target, frame_id: M.go2.frame_id, path: M.go2.path.map(r3) },
    },
    survey: { pct_mapped: pctMapped(), frames: M.frames, waypoint_idx: M.drone.wp, waypoints: SURVEY_WPS },
    grid: { ...G, cls: CLS_B64, seen: packbits(M.seen) },
    victims: M.victims,
    decisions: M.decisions,
    transcript: M.transcript,
    tts: M.tts,
    log: M.log,
    team: [{ id: "t1", name: "Rohan Deshmukh", role: "lead", lang: "hi-IN" }],
  };
}

/* ---------- HTTP ---------- */
function wav() {
  const rate = 16000, n = Math.round(rate * 0.6);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVE", 8); buf.write("fmt ", 12);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write("data", 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * i) / rate) * 8000 * Math.min(1, (n - i) / 800)), 44 + i * 2);
  return buf;
}
const WAV = wav();
const FRAME_FILES = { d: ["overhead_bottom.jpg", "overhead_payload.jpg"], g: ["go2_front_moved.jpg"] };
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".map": "application/json", ".woff2": "font/woff2" };

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "content-type": type, "access-control-allow-origin": "*", "access-control-allow-headers": "content-type", "cache-control": "no-store" });
  res.end(typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => resolve(data));
  });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = url.pathname;
  if (req.method === "OPTIONS") return send(res, 204, "");
  if (p === "/healthz") return send(res, 200, { status: "ok", version: "mock-0.1.0" });
  if (p === "/version") return send(res, 200, { version: "mock-0.1.0" });
  if (p === "/api/state") return send(res, 200, snapshot());
  if (p === "/api/command" && req.method === "POST") {
    let msg;
    try { msg = JSON.parse(await readBody(req)); } catch { return send(res, 400, { accepted: false, reason: "invalid json" }); }
    received.push({ via: "http", ...msg });
    return send(res, 200, applyMessage(msg));
  }
  if (p === "/__mock/received") return send(res, 200, received);
  if (p === "/__mock/reset" && req.method === "POST") { reset(); received = []; return send(res, 200, { ok: true }); }
  if (p === "/__mock/drop" && req.method === "POST") {
    for (const ws of clients) ws.close(1012, "mock restart");
    return send(res, 200, { dropped: clients.size });
  }
  if (p === "/__mock/advance" && req.method === "POST") {
    const s = Number(url.searchParams.get("s") || 1);
    for (let k = 0; k < s * 5; k++) tick(0.2);
    return send(res, 200, { t: M.t });
  }
  let m = p.match(/^\/frames\/f_([dg])(\d+)\.jpg$/);
  if (m) {
    const files = FRAME_FILES[m[1]];
    const file = path.join(FRAMES_DIR, files[Number(m[2]) % files.length]);
    if (existsSync(file)) return send(res, 200, readFileSync(file), "image/jpeg");
    return send(res, 404, { error: "no frame" });
  }
  m = p.match(/^\/tts\/[0-9a-f]+\.wav$/);
  if (m) return send(res, 200, WAV, "audio/wav");
  if (STATIC_DIR) {
    let file = path.join(STATIC_DIR, decodeURIComponent(p));
    if (!file.startsWith(STATIC_DIR)) return send(res, 403, "forbidden", "text/plain");
    if (!existsSync(file) || statSync(file).isDirectory()) file = path.join(STATIC_DIR, "index.html");
    if (existsSync(file)) return send(res, 200, readFileSync(file), TYPES[path.extname(file)] || "application/octet-stream");
  }
  return send(res, 404, { error: "not found" });
});

const wss = new WebSocketServer({ server, path: "/ws" });
const clients = new Set();
wss.on("connection", (ws) => {
  clients.add(ws);
  ws.send(JSON.stringify(snapshot()));
  ws.on("message", (data, isBinary) => {
    if (isBinary) {
      // zc: zero crossings in the frame; a 440 Hz tone at 16 kHz gives ~88 per 1600 samples,
      // which lets tests check the console resampled the device rate to 16 kHz.
      let zc = 0;
      for (let i = 2; i + 1 < data.length; i += 2) if ((data.readInt16LE(i) >= 0) !== (data.readInt16LE(i - 2) >= 0)) zc++;
      received.push({ binary: data.length, zc });
      onBinary(data);
      return;
    }
    let msg;
    try { msg = JSON.parse(String(data)); } catch { ws.send(JSON.stringify({ type: "event", name: "error", data: { error: "invalid_json" }, t: M.t })); return; }
    received.push(msg);
    if (msg.type === "ping") { ws.send(JSON.stringify({ type: "pong" })); return; }
    if (msg.type === "hello") return;
    const result = applyMessage(msg);
    if (!result.accepted) ws.send(JSON.stringify({ type: "event", name: "error", data: { for: msg.type, ...result }, t: M.t }));
  });
  ws.on("close", () => clients.delete(ws));
});

reset();
setInterval(() => {
  tick(0.2 * SPEED);
  const snap = JSON.stringify(snapshot());
  for (const ws of clients) if (ws.readyState === 1) ws.send(snap);
}, 200);

server.listen(PORT, HOST, () => {
  console.log(`mock backend on http://${HOST}:${PORT} (speed ${SPEED}${STATIC_DIR ? `, static ${STATIC_DIR}` : ""})`);
});
