#!/usr/bin/env node
/**
 * End-to-end tests of the console against tests/mock-server.mjs (docs/PROTOCOL.md).
 *
 *   npm run test:e2e            # vite build, then this file
 *   E2E_ONLY=voice node tests/e2e.cjs
 *
 * Starts the mock on a free port serving dist/ (same origin, as the backend does), drives
 * Chromium and asserts both on the page and on what the mock received. Screenshots go to
 * tests/screenshots/. Playwright resolution is the same as tests/smoke.cjs: the project's
 * @playwright/test when its Chromium is installed, otherwise a machine-wide `playwright`.
 */
"use strict";

const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");
const SHOTS = path.join(__dirname, "screenshots");
const ONLY = process.env.E2E_ONLY || "";

function candidates() {
  const list = [];
  if (process.env.PLAYWRIGHT_MODULE) list.push(process.env.PLAYWRIGHT_MODULE);
  list.push("@playwright/test", "playwright");
  for (const dir of (process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean)) list.push(path.join(dir, "playwright"));
  return [...new Set(list)];
}

async function launchChromium() {
  const errors = [];
  for (const name of candidates()) {
    let mod;
    try {
      mod = require(name);
    } catch {
      errors.push(`${name}: not installed`);
      continue;
    }
    try {
      return { browser: await mod.chromium.launch(), name };
    } catch (err) {
      errors.push(`${name}: ${String(err.message).split("\n")[0]}`);
    }
  }
  throw new Error(`No usable Playwright Chromium:\n  ${errors.join("\n  ")}`);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, what, timeout = 15_000, every = 100) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try {
      last = await fn();
      if (last) return last;
    } catch (err) {
      last = err;
    }
    await sleep(every);
  }
  throw new Error(`timed out waiting for ${what} (last: ${last instanceof Error ? last.message : JSON.stringify(last)})`);
}

function check(cond, message) {
  if (!cond) throw new Error(`FAIL: ${message}`);
  console.log(`  ok   ${message}`);
}

/* Fake microphone: getUserMedia returns a 440 Hz tone from a real AudioContext, so the
   console's AudioWorklet capture, resampler and int16 framing run for real. */
const FAKE_MIC = `
  (() => {
    const md = navigator.mediaDevices || (navigator.mediaDevices = {});
    md.getUserMedia = async () => {
      const ctx = new AudioContext();
      await ctx.resume();
      const osc = ctx.createOscillator();
      osc.frequency.value = 440;
      const dest = ctx.createMediaStreamDestination();
      osc.connect(dest);
      osc.start();
      window.__fakeMic = { ctx, osc, rate: ctx.sampleRate };
      return dest.stream;
    };
  })();
`;
const NO_WORKLET = `Object.defineProperty(BaseAudioContext.prototype, "audioWorklet", { get() { return undefined; } });`;

async function main() {
  fs.mkdirSync(SHOTS, { recursive: true });
  if (!fs.existsSync(path.join(ROOT, "dist", "index.html"))) throw new Error("dist/ missing: run `npm run build` first");
  const port = await freePort();
  const BASE = `http://127.0.0.1:${port}`;
  const mock = spawn(process.execPath, [path.join(__dirname, "mock-server.mjs"), "--port", String(port), "--static", "dist", "--speed", "4"], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "inherit"],
  });
  mock.stdout.on("data", (d) => process.stdout.write(`  mock ${d}`));
  const api = async (p, method = "GET") => (await fetch(`${BASE}${p}`, { method })).json();
  await waitFor(() => api("/healthz").then((b) => b.status === "ok"), "mock /healthz", 10_000);
  const received = () => api("/__mock/received");
  const sent = async (pred) => (await received()).filter((m) => m.binary == null).find(pred);

  const { browser, name } = await launchChromium();
  console.log(`info playwright module: ${name}, chromium ${browser.version()}`);
  const pageErrors = [];
  const results = [];

  async function newPage(width, height, init = []) {
    const ctx = await browser.newContext({ viewport: { width, height } });
    for (const s of init) await ctx.addInitScript(s);
    const page = await ctx.newPage();
    page.on("pageerror", (err) => pageErrors.push(`[${width}] ${err}`));
    page.on("console", (msg) => {
      if (msg.type() === "error") pageErrors.push(`[${width}] console.error: ${msg.text()}`);
    });
    return { ctx, page };
  }
  const stats = (page) => page.evaluate(() => window.__skypaw.mapStats());
  const snap = (page) => page.evaluate(() => window.__skypaw.snap());
  /** Clicks an entity on the map; if a tile or card covers it, drags the map (pan) to bring it to the centre first. */
  async function clickEntity(page, kind, id) {
    const project = () => waitFor(() => page.evaluate(([k, i]) => window.__skypaw.project(k, i), [kind, id]), `${kind} ${id ?? ""} on the map`);
    const box = await page.locator("#map").boundingBox();
    let p = await project();
    const onMap = (q) => page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.id === "map", [box.x + q.x, box.y + q.y]);
    if (!(await onMap(p))) {
      let c = null;
      for (const [fx, fy] of [[0.5, 0.4], [0.35, 0.3], [0.3, 0.5], [0.5, 0.2], [0.2, 0.25], [0.6, 0.6]]) {
        const q = { x: box.width * fx, y: box.height * fy };
        if (await onMap(q)) {
          c = { x: box.x + q.x, y: box.y + q.y };
          break;
        }
      }
      if (!c) throw new Error('no free map area to pan from');
      await page.mouse.move(c.x, c.y);
      await page.mouse.down();
      await page.mouse.move(c.x + (c.x - box.x - p.x) / 2, c.y + (c.y - box.y - p.y) / 2, { steps: 4 });
      await page.mouse.move(c.x + (c.x - box.x - p.x), c.y + (c.y - box.y - p.y), { steps: 4 });
      await page.mouse.up();
      p = await project();
      const blocker = await page.evaluate(([x, y]) => { const e = document.elementFromPoint(x, y); return e ? `${e.tagName}#${e.id}.${e.className}` : "none"; }, [box.x + p.x, box.y + p.y]);
      check(await onMap(p), `panned the map to uncover ${kind} ${id ?? ""} (at ${Math.round(p.x)},${Math.round(p.y)}: ${blocker})`);
    }
    await page.mouse.click(box.x + p.x, box.y + p.y);
  }
  async function noHScroll(page, label) {
    const [sw, cw, bw, sl] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth, document.body.scrollWidth, document.getElementById("stage").scrollLeft]);
    check(sw <= cw && bw <= cw && sl === 0, `${label}: no horizontal scroll (scrollWidth ${sw}, body ${bw}, viewport ${cw}, stage scrollLeft ${sl})`);
  }

  async function test(id, title, fn) {
    if (ONLY && !id.includes(ONLY)) return;
    console.log(`\n${title}`);
    try {
      await fn();
      results.push({ id, ok: true });
    } catch (err) {
      console.log(`  FAIL ${err.message}`);
      results.push({ id, ok: false, err: err.message });
    }
  }

  await api("/__mock/reset", "POST");
  const { ctx: mainCtx, page } = await newPage(1280, 720, [FAKE_MIC, "localStorage.setItem('skypaw_token','lead-secret')"]);
  try {
    await test("connect", "Connects to /ws, sends hello as lead with the stored token, renders the snapshot", async () => {
      await page.goto(`${BASE}/`, { waitUntil: "load" });
      check((await page.title()) === "Sky & Paw Ops", "page title");
      await page.waitForSelector('body[data-conn="connected"]', { timeout: 10_000 });
      check((await page.textContent(".status-text")) === "Sky & Paw Ops · connected", "status text (T0 smoke contract)");
      const hello = await waitFor(() => sent((m) => m.type === "hello"), "hello");
      check(hello.role === "lead" && hello.token === "lead-secret", `hello ${JSON.stringify(hello)}`);
      await waitFor(async () => (await stats(page)).robots === 2, "two robots drawn");
      check(true, "drone and Go2 drawn from robots.*.pos");
      await waitFor(async () => /SIM ready/i.test(await page.textContent("#simChip")), "SIM ready chip");
      check(true, "header shows sim status");
    });

    await test("map", "Map shows two robots, the survey and 3 victims with triage colour and ?", async () => {
      await waitFor(async () => (await stats(page)).surveyWaypoints > 0, "survey waypoints drawn", 15_000);
      check(true, "survey pattern drawn while the drone surveys");
      await page.screenshot({ path: path.join(SHOTS, "01-survey-1280.png") });
      await waitFor(async () => (await stats(page)).victims === 3, "3 victims", 30_000);
      const st = await stats(page);
      check(st.robots === 2 && st.victims === 3, `map stats ${JSON.stringify(st)}`);
      check(st.seenCells > 1000, `seen grid decoded (${st.seenCells} cells seen)`);
      const kImm = await page.textContent("#kImm");
      const kDel = await page.textContent("#kDel");
      const kUnk = await page.textContent("#kUnk");
      check(kImm === "1" && kDel === "1" && kUnk === "1", `header triage counts from suggestions ${kImm}/${kDel}/${kUnk}`);
    });

    await test("decision", "Decision card appears with Confirm · Go and Cancel", async () => {
      await page.waitForSelector(".dec .btn.primary", { timeout: 20_000 });
      const title = await page.textContent(".dec .dec-t");
      check(/V-01/.test(title), `card title "${title}"`);
      check((await page.textContent(".dec .dec-k")) === "Confirm robot plan", "kind label");
      const labels = await page.$$eval(".dec .dec-a .btn", (bs) => bs.map((b) => b.textContent));
      check(labels.join("|") === "Confirm · Go|Cancel", `actions ${labels.join(", ")}`);
      await page.screenshot({ path: path.join(SHOTS, "02-decision-card-1280.png") });
    });

    await test("panel", "Victim panel: opens within 200 ms, triage sends triage, Send Go2 sends command, Speak sends tts", async () => {
      const t0 = Date.now();
      await clickEntity(page, "victim", "V-02");
      await page.waitForSelector('.stage.panel .panel-v[data-id="V-02"]', { timeout: 2000 });
      const dt = Date.now() - t0;
      check(dt < 200, `context panel open in ${dt} ms (A1)`);
      for (const sel of [".vid", ".chip", ".triage", ".needs", ".sendrow", "details.ev-d"]) check(await page.$(`.cpanel ${sel}`), `panel has ${sel}`);
      check(!(await page.$eval(".cpanel details.ev-d", (d) => d.open)), "evidence collapsed by default");
      check(await page.$('.cpanel .tbtn.t-delayed.sugg'), "suggested triage has the dashed style");
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "03-victim-panel-1280.png") });
      await page.click('.cpanel [data-tri="immediate"]');
      const tri = await waitFor(() => sent((m) => m.type === "triage"), "triage message");
      check(tri.victim_id === "V-02" && tri.category === "immediate", `sent ${JSON.stringify(tri)}`);
      await waitFor(async () => (await page.textContent("#kImm")) === "2", "Immediate count 2");
      check(await page.$('.cpanel .tbtn.t-immediate.on'), "chosen triage filled; header count updated (C4)");
      check(/suggested/.test(await page.textContent('.cpanel .tbtn.t-delayed')), "the system suggestion stays visible after the lead's choice (C4)");
      await waitFor(async () => (await snap(page)).victims.find((v) => v.id === "V-02").triage === "immediate", "snapshot confirms triage");
      check(true, "pin label drops the ? once the snapshot carries the lead's triage");
      await page.click('.cpanel [data-act="go2"]');
      const cmd = await waitFor(() => sent((m) => m.type === "command" && m.intent === "navigate_go2"), "navigate_go2 command");
      check(cmd.target === "V-02", `sent ${JSON.stringify(cmd)}`);
      await page.click('.cpanel [data-ph="p_hear"]');
      const tts = await waitFor(() => sent((m) => m.type === "tts" && m.phrase_id === "p_hear"), "tts phrase");
      check(tts.lang === "mr-IN", `sent ${JSON.stringify(tts)}`);
      await page.waitForSelector(".tts-badge.on", { timeout: 3000 });
      check(/Go2 speaking · मराठी · rohan/.test(await page.textContent(".tts-badge")), "TTS badge shows language and voice (B4)");
      await waitFor(async () => (await snap(page)).log.some((e) => e.src === "TTS" && /तुम्हाला ऐकू येतंय का/.test(e.text["en-IN"]) && /mr-IN/.test(e.text["en-IN"])), "TTS log line");
      check(true, "log carries the exact spoken text and language (B4, from the snapshot)");
      const audioSrc = await waitFor(() => page.$eval("#ttsAudio", (a) => a.getAttribute("src")), "audio src");
      check(/\/tts\/[0-9a-f]+\.wav$/.test(audioSrc), `<audio> plays ${audioSrc} after the first gesture`);
      await page.fill(".cpanel [data-relay]", "Stay where you are");
      await page.press(".cpanel [data-relay]", "Enter");
      const relay = await waitFor(() => sent((m) => m.type === "tts" && m.text === "Stay where you are"), "tts relay text");
      check(!!relay, "relay field sends tts with text");
      await page.keyboard.press("Escape");
      await waitFor(async () => !(await page.$(".stage.panel")), "panel closed by Esc");
      check(true, "Esc closes the panel");
    });

    await test("confirm", "Confirm on the plan card sends {type: confirm, plan_id}; Cancel sends cancel", async () => {
      const cards = await page.$$eval(".dec", (els) => els.map((e) => e.querySelector(".dec-t").textContent));
      check(cards.length >= 2, `queue shows ${cards.length} cards newest first: ${cards.join(" | ")}`);
      const v02 = page.locator(".dec", { hasText: "Go2 → V-02" });
      await v02.locator('[data-dec="cancel"]').click();
      const cancel = await waitFor(() => sent((m) => m.type === "cancel"), "cancel");
      check(cancel.plan_id === "P-2" || /^P-/.test(cancel.plan_id), `sent ${JSON.stringify(cancel)}`);
      const v01 = page.locator(".dec", { hasText: "Go2 → V-01" });
      check((await snap(page)).robots.go2.mode !== "moving", "Go2 does not move while the plan waits (B1)");
      check((await stats(page)).goPath === 0, "no solid Go2 path before Confirm");
      await v01.locator(".btn.primary").click();
      const confirm = await waitFor(() => sent((m) => m.type === "confirm"), "confirm");
      check(confirm.plan_id === "P-1", `sent ${JSON.stringify(confirm)}`);
      await waitFor(async () => (await stats(page)).goPath > 0, "Go2 path drawn while moving", 10_000);
      check(true, "Go2 path drawn solid after confirm");
      await page.screenshot({ path: path.join(SHOTS, "04-go2-moving-1280.png") });
    });

    await test("cams", "Camera tiles: src follows frame_id; tap → focus mode; Esc → back", async () => {
      const first = await page.getAttribute('.cam[data-cam="drone"] img', "src");
      check(/\/frames\/f_d\d+\.jpg$/.test(first), `drone tile src ${first}`);
      const next = await waitFor(async () => {
        const s = await page.getAttribute('.cam[data-cam="drone"] img', "src");
        return s !== first ? s : null;
      }, "drone frame change", 5000);
      const fid = (await snap(page)).robots.drone.frame_id;
      check(next.endsWith(`${fid}.jpg`) || /f_d\d+\.jpg$/.test(next), `src changed to ${next} with frame_id`);
      const go2src = await page.getAttribute('.cam[data-cam="go2"] img', "src");
      check(/\/frames\/f_g\d+\.jpg$/.test(go2src), `Go2 tile src ${go2src}`);
      await waitFor(() => page.$eval('.cam[data-cam="go2"] img', (i) => i.complete && i.naturalWidth > 0), "Go2 frame decoded");
      await page.click('.cam[data-cam="go2"]');
      await page.waitForSelector(".stage.focus .cam.focus", { timeout: 2000 });
      const mini = await page.$eval("#map", (c) => [c.clientWidth, c.clientHeight]);
      check(mini[0] === 300 && mini[1] === 200, `map is a 300×200 minimap in focus mode (${mini.join("×")}) (G1)`);
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "05-focus-mode-1280.png") });
      await noHScroll(page, "focus mode");
      await page.click('.cam[data-cam="go2"]');
      await waitFor(async () => !(await page.$(".stage.focus")), "focus off on second tap");
      await page.click('.cam[data-cam="drone"]');
      await page.waitForSelector(".stage.focus", { timeout: 2000 });
      await page.keyboard.press("Escape");
      await waitFor(async () => !(await page.$(".stage.focus")), "focus off on Esc");
      check(true, "second tap and Esc restore the layout");
    });

    await test("voice", "Hold-to-talk: mic start, ≥ 1 binary 3200-byte frame, mic stop; partial and final transcript", async () => {
      const before = (await received()).length;
      const box = await page.locator("#ptt").boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await waitFor(() => page.$("#ptt.live"), "PTT live");
      await waitFor(async () => (await received()).slice(before).filter((m) => m.binary).length >= 4, "binary frames", 5000);
      await waitFor(async () => /\S/.test((await page.textContent("#vline .partial")) || "") && (await page.textContent("#vline .partial")) !== "…", "partial transcript", 5000);
      await page.screenshot({ path: path.join(SHOTS, "06-ptt-live-1280.png") });
      await noHScroll(page, "after panel and tile interactions");
      await page.mouse.up();
      await waitFor(() => sent((m) => m.type === "mic" && m.state === "stop"), "mic stop");
      const log = (await received()).slice(before);
      const startIdx = log.findIndex((m) => m.type === "mic" && m.state === "start");
      const stopIdx = log.findIndex((m) => m.type === "mic" && m.state === "stop");
      const frames = log.filter((m, i) => m.binary != null && i > startIdx && i < stopIdx);
      check(startIdx >= 0 && log[startIdx].lang === "auto", `mic start ${JSON.stringify(log[startIdx])}`);
      check(frames.length >= 1, `${frames.length} binary frames between start and stop`);
      check(frames.slice(0, -1).every((f) => f.binary === 3200), `full frames are 3200 bytes (${[...new Set(frames.map((f) => f.binary))].join(",")})`);
      check(stopIdx > startIdx && !log.slice(stopIdx + 1).some((m) => m.binary != null), "no binary after mic stop");
      const zcs = frames.filter((f) => f.binary === 3200).slice(1).map((f) => f.zc);
      check(zcs.length > 0 && zcs.every((z) => z >= 80 && z <= 96), `440 Hz tone arrives as 440 Hz at 16 kHz (zero crossings per frame ${zcs.join(",")}, expect ~88)`);
      const rate = await page.evaluate(() => window.__fakeMic && window.__fakeMic.rate);
      console.log(`  info device rate ${rate} Hz resampled to 16 kHz`);
      await waitFor(async () => /V-02/.test(await page.textContent("#vline")), "final transcript in the strip");
      check(/hi-IN/.test(await page.textContent("#vline")), "final kept with its language code");
    });

    await test("typed", "Typed fallback sends text_command", async () => {
      await page.click("#vtoggle");
      await page.waitForSelector(".vmore.on");
      await page.fill("#cmdText", "ड्रोन से फिर से मैप करो");
      await page.press("#cmdText", "Enter");
      const tc = await waitFor(() => sent((m) => m.type === "text_command"), "text_command");
      check(tc.text === "ड्रोन से फिर से मैप करो" && tc.lang === "hi-IN", `sent ${JSON.stringify(tc)}`);
      check(!(await page.$(".vmore.on")), "menu closes after sending");
    });

    await test("tts", "Go2 arrives: speaker card and TTS badge; Go2 speaker card action sends decide", async () => {
      const card = page.locator(".dec.k-speak");
      await card.first().waitFor({ timeout: 20_000 });
      check((await card.first().locator(".dec-k").textContent()) === "Go2 speaker", "Go2 speaker card");
      await page.waitForSelector(".tts-badge.on", { timeout: 5000 });
      await page.screenshot({ path: path.join(SHOTS, "07-go2-arrived-tts-1280.png") });
      await card.first().locator('[data-dec="p_medic"]').click();
      const d = await waitFor(() => sent((m) => m.type === "decide"), "decide");
      check(d.action === "p_medic" && /^D-/.test(d.decision_id), `sent ${JSON.stringify(d)}`);
    });

    await test("robots", "Robot panels: drone Survey/Hold/Land and Go2 Stop send commands", async () => {
      await clickEntity(page, "drone");
      await page.waitForSelector(".cpanel .panel-t", { timeout: 2000 });
      check(/battery/.test(await page.textContent(".cpanel .state")) && /pose age/.test(await page.textContent(".cpanel .state")), "drone mode, battery, pose age");
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "08-drone-panel-1280.png") });
      await page.click('.cpanel [data-cmd="land"]');
      check(await waitFor(() => sent((m) => m.type === "command" && m.intent === "land"), "land"), "Land → command land");
      await page.click('.cpanel [data-cmd="aerial_survey"]');
      check(await waitFor(() => sent((m) => m.type === "command" && m.intent === "aerial_survey"), "survey"), "Survey → command aerial_survey");
      await page.click('.cpanel [data-cmd="stop_all"]');
      const hold = await waitFor(() => sent((m) => m.type === "command" && m.intent === "stop_all"), "hold");
      check(hold.target === "drone", `Hold → ${JSON.stringify(hold)}`);
      await page.keyboard.press("Escape");
      await clickEntity(page, "go2");
      await page.waitForSelector(".cpanel .phrases", { timeout: 2000 });
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "09-go2-panel-1280.png") });
      await page.selectOption(".cpanel [data-lang]", "ta-IN");
      await page.click('.cpanel [data-ph="p_still"]');
      const t = await waitFor(() => sent((m) => m.type === "tts" && m.phrase_id === "p_still"), "Go2 phrase");
      check(t.lang === "ta-IN", `Go2 speaker phrase in the chosen language ${JSON.stringify(t)}`);
      await page.click('.cpanel [data-cmd="stop_all"][data-target="go2"]');
      const stops = (await received()).filter((m) => m.type === "command" && m.intent === "stop_all");
      check(stops.some((m) => m.target === "go2"), "Stop → command stop_all target go2");
      await page.keyboard.press("Escape");
    });

    await test("dismiss", "Dismiss sends dismiss; pin shows DISMISSED", async () => {
      await clickEntity(page, "victim", "V-03");
      await page.waitForSelector('.panel-v[data-id="V-03"]');
      await page.click('.cpanel [data-act="dismiss"]');
      const d = await waitFor(() => sent((m) => m.type === "dismiss"), "dismiss");
      check(d.victim_id === "V-03", `sent ${JSON.stringify(d)}`);
      await waitFor(async () => (await snap(page)).victims.find((v) => v.id === "V-03").status === "dismissed", "dismissed in snapshot");
      check((await page.textContent("#kUnk")) === "0", "dismissed victim leaves the counts");
      await page.keyboard.press("Escape");
    });

    await test("log", "Log overlay in the selected language; Esc closes", async () => {
      await page.click("#tLog");
      await page.waitForSelector("#ovlLog.on");
      const en = await page.textContent("#log");
      check(/Lead approved/.test(en), "English log lines");
      await page.selectOption("#logLang", "hi-IN");
      await waitFor(async () => /लीड/.test(await page.textContent("#log")), "Hindi log lines");
      check(true, "log text switches to log[].text['hi-IN']");
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "10-log-overlay-hi-1280.png") });
      await page.keyboard.press("Escape");
      await waitFor(async () => !(await page.$("#ovlLog.on")), "overlay closed");
      check(true, "Esc closes the overlay");
    });

    await test("settings", "Settings popover holds backend URL and token; Esc closes", async () => {
      await page.click("#btnSettings");
      await page.waitForSelector("#settingsPop.on");
      check((await page.inputValue("#tokenInput")) === "lead-secret", "token field shows the stored token");
      check((await page.getAttribute("#tokenInput", "type")) === "password", "token field is a password input");
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(SHOTS, "11-settings-1280.png") });
      await page.keyboard.press("Escape");
      check(!(await page.$("#settingsPop.on")), "Esc closes settings");
    });

    await test("layers", "Layers popover toggles a layer; Esc closes", async () => {
      await page.click("#tLayers");
      await page.waitForSelector("#layersPop.on");
      await page.uncheck('[data-layer="victims"]');
      await waitFor(async () => (await stats(page)).victims === 0, "people layer off");
      await page.check('[data-layer="victims"]');
      await waitFor(async () => (await stats(page)).victims === 3, "people layer on");
      check(true, "People layer toggles");
      await page.keyboard.press("Escape");
      check(!(await page.$("#layersPop.on")), "Esc closes layers");
    });

    await test("focus", "Visible focus state on keyboard focus", async () => {
      await page.focus("#tLayers");
      await page.keyboard.press("Tab");
      const outline = await page.evaluate(() => {
        const el = document.activeElement;
        const cs = getComputedStyle(el);
        return { id: el.id, style: cs.outlineStyle, width: cs.outlineWidth, color: cs.outlineColor };
      });
      check(outline.style !== "none" && outline.width !== "0px", `focused #${outline.id} outline ${outline.style} ${outline.width} ${outline.color}`);
      await page.focus('.cam[data-cam="drone"]');
      await page.keyboard.press("Tab");
      await page.keyboard.press("Shift+Tab");
      const camOutline = await page.$eval('.cam[data-cam="drone"]', (el) => getComputedStyle(el).outlineStyle);
      check(camOutline !== "none", "camera tile has a focus outline");
    });
  } finally {
    await mainCtx.close();
  }

  await test("fallback", "ScriptProcessor fallback (no AudioWorklet) still streams 3200-byte frames", async () => {
    const { ctx, page: p2 } = await newPage(1280, 720, [FAKE_MIC, NO_WORKLET]);
    try {
      await p2.goto(`${BASE}/`);
      await p2.waitForSelector('body[data-conn="connected"]');
      const before = (await received()).length;
      const box = await p2.locator("#ptt").boundingBox();
      await p2.mouse.move(box.x + 20, box.y + 20);
      await p2.mouse.down();
      await waitFor(async () => (await received()).slice(before).filter((m) => m.binary === 3200).length >= 2, "fallback binary frames", 5000);
      await p2.mouse.up();
      await waitFor(async () => (await received()).slice(before).some((m) => m.type === "mic" && m.state === "stop"), "mic stop");
      check(true, "ScriptProcessor path sends 3200-byte frames");
    } finally {
      await ctx.close();
    }
  });

  await test("widths", "No horizontal scroll at 1280, 1500 and 360 px (H1); screenshots", async () => {
    for (const [w, h] of [[1280, 800], [1500, 900], [360, 740]]) {
      const { ctx, page: p } = await newPage(w, h);
      try {
        await p.goto(`${BASE}/`);
        await p.waitForSelector('body[data-conn="connected"]');
        await waitFor(async () => (await stats(p)).victims >= 2, "victims", 10_000);
        await noHScroll(p, `${w} px`);
        await p.screenshot({ path: path.join(SHOTS, `20-console-${w}.png`) });
        await clickEntity(p, "victim", "V-01");
        await p.waitForSelector(".stage.panel .panel-v");
        await noHScroll(p, `${w} px with the context panel`);
        await p.waitForTimeout(300);
        await p.screenshot({ path: path.join(SHOTS, `21-console-${w}-panel.png`) });
        await p.keyboard.press("Escape");
        await p.click("#tLog");
        await noHScroll(p, `${w} px with the log overlay`);
        await p.keyboard.press("Escape");
        await p.click("#vtoggle");
        await noHScroll(p, `${w} px with the command menu`);
        if (w === 360) await p.screenshot({ path: path.join(SHOTS, "22-console-360-vmore.png") });
        await p.keyboard.press("Escape");
      } finally {
        await ctx.close();
      }
    }
  });

  await test("pages", "Console on another origin (GitHub Pages) with ?api= connects, loads frames cross-origin, remembers the URL", async () => {
    const http = require("node:http");
    const dist = path.join(ROOT, "dist");
    const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff" };
    const srv = http.createServer((req, res) => {
      let file = path.join(dist, decodeURIComponent(new URL(req.url, "http://x").pathname));
      if (!file.startsWith(dist) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(dist, "index.html");
      res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
      res.end(fs.readFileSync(file));
    });
    const sport = await freePort();
    await new Promise((r) => srv.listen(sport, "127.0.0.1", r));
    const STATIC = `http://localhost:${sport}`;
    const { ctx, page: p } = await newPage(1280, 720);
    try {
      await p.goto(`${STATIC}/?api=${encodeURIComponent(BASE)}`);
      await p.waitForSelector('body[data-conn="connected"]', { timeout: 10_000 });
      check(true, `${STATIC} with ?api=${BASE} connects over /ws`);
      check((await p.evaluate(() => localStorage.getItem("skypaw_api"))) === BASE, "?api= remembered in localStorage.skypaw_api");
      const src = await waitFor(() => p.getAttribute('.cam[data-cam="drone"] img', "src"), "frame src");
      check(src.startsWith(`${BASE}/frames/`), `frames load from the backend origin (${src})`);
      await waitFor(() => p.$eval('.cam[data-cam="drone"] img', (i) => i.complete && i.naturalWidth > 0), "cross-origin frame decoded");
      await waitFor(async () => (await p.textContent("#versionDd")) === "mock-0.1.0", "version via cross-origin /healthz");
      check(true, "cross-origin /healthz allowed by CORS");
      await p.goto(`${STATIC}/`);
      await p.waitForSelector('body[data-conn="connected"]', { timeout: 10_000 });
      check(/\(storage\)$/.test(await p.textContent(".meta dd")), "reload without ?api= uses the stored backend");
    } finally {
      await ctx.close();
      srv.close();
    }
  });

  await test("reconnect", "Reconnects after the socket drops", async () => {
    const { ctx, page: p } = await newPage(1280, 720);
    try {
      await p.goto(`${BASE}/`);
      await p.waitForSelector('body[data-conn="connected"]');
      const hellos = (await received()).filter((m) => m.type === "hello").length;
      await api("/__mock/drop", "POST");
      await p.waitForSelector('body[data-conn="offline"]', { timeout: 3000 });
      check(/retrying in/.test(await p.textContent(".status-text")), "offline state with retry countdown");
      await p.waitForSelector('body[data-conn="connected"]', { timeout: 5000 });
      const after = (await received()).filter((m) => m.type === "hello").length;
      check(after > hellos, "reconnected and sent hello again");
    } finally {
      await ctx.close();
    }
  });

  await test("errors", "No uncaught errors or console errors (H2)", async () => {
    check(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  });

  await browser.close();
  mock.kill();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  for (const f of failed) console.log(`  FAILED ${f.id}: ${f.err}`);
  console.log(`screenshots: ${path.relative(ROOT, SHOTS)}/`);
  if (failed.length) {
    console.log("E2E FAIL");
    process.exit(1);
  }
  console.log("E2E PASS");
}

main().catch((err) => {
  console.error(String(err && err.stack ? err.stack : err));
  console.error("E2E FAIL");
  process.exit(1);
});
