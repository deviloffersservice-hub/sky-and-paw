import "./fonts";
import "./styles.css";

import { initAudio } from "./audio";
import { type Backend, httpUrl, normaliseBackend, resolveBackend, storeBackend } from "./backend";
import { initCams } from "./cams";
import { initLog } from "./log";
import { LAYERS, TacticalMap, type LayerKey } from "./map";
import { initPanel } from "./panel";
import { initQueue } from "./queue";
import { esc, fmtHms, fmtMs, SITE, store } from "./state";
import { initVoice } from "./voice";
import { Connection, type ConnState, readToken, storeToken } from "./ws";

const TITLE = "Sky & Paw Ops";

const LEGEND =
  '<hr><div class="lg"><i style="background:#1d3a2a"></i>Passable <i style="background:#4a3a14;margin-left:6px"></i>Climb <i style="background:#3a1a1a;margin-left:6px"></i>Blocked <i style="background:#141b23;margin-left:6px"></i>Unmapped</div>' +
  '<div class="lg"><i style="background:var(--bad);border-radius:50%"></i>Immediate <i style="background:var(--warn);border-radius:50%;margin-left:6px"></i>Delayed <i style="background:#9fb0c0;border-radius:50%;margin-left:6px"></i>Unknown</div>' +
  '<div class="lg"><i style="background:var(--drone)"></i>Drone <i style="background:var(--go2);margin-left:6px"></i>Go2</div>';

function markup(backend: Backend): string {
  return `
<header class="top">
  <div class="brand">SKY &amp; PAW<small>Ops · simulation</small></div>
  <div class="site"><b id="siteName">${esc(SITE.name)}</b><span id="siteDesc">${esc(SITE.desc)}</span></div>
  <div class="clocks"><span>Mission<b id="stT">00:00</b></span><span class="since">Since collapse<b id="stSince">—</b></span></div>
  <div class="tri" aria-label="Triage counts"><span class="imm"><b id="kImm">0</b><small>Immediate</small></span><span class="del"><b id="kDel">0</b><small>Delayed</small></span><span><b id="kUnk">0</b><small>Unknown</small></span><span class="ext"><b id="kExt">0</b><small>Extracted</small></span></div>
  <div class="ctrl">
    <div class="status" role="status" aria-live="polite"><i></i><span class="conn-label">Connecting…</span></div>
    <span class="chip simchip" id="simChip" title="Simulation">SIM —</span>
    <button class="btn tiny" id="btnSettings" aria-expanded="false" aria-controls="settingsPop" title="Backend and token">Settings</button>
  </div>
</header>
<main class="stage" id="stage">
  <canvas class="map" id="map" role="img" aria-label="Tactical map: passability grid, robots and people"></canvas>
  <div class="tools"><button class="btn" id="tLayers" aria-expanded="false" aria-controls="layersPop">Layers ▾</button><button class="btn" id="tLog" aria-controls="ovlLog">Log</button><button class="btn" id="tReset" title="Reset view" aria-label="Reset view">⌖</button></div>
  <div class="pop" id="layersPop" role="group" aria-label="Map layers">${LAYERS.map(([k, label]) => `<label><input type="checkbox" data-layer="${k}" checked> ${label}</label>`).join("")}${LEGEND}</div>
  <div class="queue" id="queue" aria-label="Decision queue"></div>
  <div class="cams" id="cams"></div>
  <div class="tts-badge" id="ttsBadge" role="status"></div>
  <div class="vmore" id="vmore"></div>
  <div class="voice"><button class="ptt" id="ptt" aria-pressed="false" aria-label="Hold to talk">Hold</button><div class="vtext"><div class="line" id="vline"></div><div class="sub" id="vsub"></div></div><button class="btn tiny" id="vtoggle">▾</button></div>
  <aside class="cpanel" id="cpanel" aria-label="Details" aria-labelledby="cpTitle"></aside>
  <div class="ovl" id="ovlLog" role="dialog" aria-modal="true" aria-labelledby="logTitle"><div class="ovl-box"><div class="ovl-h"><h2 id="logTitle">Mission log</h2><div><select class="sel" id="logLang" aria-label="Language"></select> <button class="btn" data-close>Close</button></div></div><ul class="log" id="log"></ul></div></div>
  <div class="pop settings" id="settingsPop" role="dialog" aria-label="Connection settings">
    <form class="backend" id="settingsForm">
      <label for="apiInput">Backend</label>
      <input class="sel mono" id="apiInput" type="url" name="api" placeholder="same origin" spellcheck="false" value="${backend.source === "same-origin" ? "" : esc(backend.base)}">
      <label for="tokenInput">Lead token</label>
      <input class="sel mono" id="tokenInput" type="password" name="token" placeholder="none (dev)" autocomplete="off" value="${esc(readToken())}">
      <div class="row"><button class="btn primary" type="submit">Save &amp; reconnect</button><button class="btn" type="button" data-close-pop>Close</button></div>
    </form>
    <p class="status-text mono">${TITLE} · connecting…</p>
    <dl class="meta mono"><dt>backend</dt><dd>${esc(`${backend.base} (${backend.source})`)}</dd><dt>version</dt><dd id="versionDd">…</dd></dl>
  </div>
</main>
<audio id="ttsAudio" preload="none"></audio>`;
}

function $(sel: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(sel);
  if (!el) throw new Error(`missing ${sel}`);
  return el;
}

function boot(root: HTMLElement): void {
  const backend = resolveBackend();
  root.innerHTML = markup(backend);
  const conn = new Connection(backend);
  const stage = $("#stage");
  // The stage is overflow:hidden but still scrollable by focus() / scrollIntoView on a child
  // (the off-canvas panel, a focused tile); pin it so the layout never shifts sideways.
  stage.addEventListener("scroll", () => {
    if (stage.scrollLeft || stage.scrollTop) stage.scrollTo(0, 0);
  });

  // connection status (text kept as T0's so tests/smoke.cjs still applies)
  const statusEl = $(".status");
  const statusText = $(".status-text");
  const connLabel = $(".conn-label");
  const versionDd = $("#versionDd");
  const loadVersion = async () => {
    try {
      const resp = await fetch(httpUrl(backend, "/healthz"), { cache: "no-store" });
      const body = (await resp.json()) as { version?: string };
      versionDd.textContent = body.version ?? "unknown";
    } catch {
      versionDd.textContent = "unreachable";
    }
  };
  conn.onState((state: ConnState, detail: string) => {
    document.body.dataset.conn = state;
    statusEl.dataset.state = state;
    statusText.textContent = `${TITLE} · ${state === "connecting" ? "connecting…" : state}${detail ? ` · ${detail}` : ""}`;
    connLabel.textContent = state === "connected" ? "Live" : state === "connecting" ? "Connecting…" : "Offline";
    statusEl.title = statusText.textContent;
    if (state === "connected") void loadVersion();
  });

  // settings popover: backend URL + token
  const settingsPop = $("#settingsPop");
  const btnSettings = $("#btnSettings");
  const setSettings = (on: boolean) => {
    settingsPop.classList.toggle("on", on);
    btnSettings.setAttribute("aria-expanded", String(on));
    if (on) ($("#apiInput") as HTMLInputElement).focus({ preventScroll: true });
  };
  btnSettings.addEventListener("click", () => setSettings(!settingsPop.classList.contains("on")));
  settingsPop.querySelector("[data-close-pop]")?.addEventListener("click", () => setSettings(false));
  $("#settingsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#apiInput") as HTMLInputElement;
    const value = normaliseBackend(input.value);
    if (input.value.trim() && !value) {
      input.setCustomValidity("Enter an http(s) URL, e.g. https://name-7860.app.github.dev");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    storeBackend(value);
    storeToken(($("#tokenInput") as HTMLInputElement).value.trim());
    const next = new URL(window.location.href);
    next.searchParams.delete("api");
    window.location.assign(next.toString());
  });

  // map + layers
  const map = new TacticalMap($("#map") as HTMLCanvasElement, () => {
    store.select(null);
    closePops();
  });
  const layersPop = $("#layersPop");
  const tLayers = $("#tLayers");
  tLayers.addEventListener("click", () => tLayers.setAttribute("aria-expanded", String(layersPop.classList.toggle("on"))));
  layersPop.addEventListener("change", (e) => {
    const c = (e.target as HTMLElement).closest<HTMLInputElement>("[data-layer]");
    if (c) map.setLayer(c.dataset.layer as LayerKey, c.checked);
  });

  const cams = initCams($("#cams"), stage, backend);
  $("#tReset").addEventListener("click", () => {
    map.reset();
    cams.setFocus(null);
  });

  // log overlay
  const ovlLog = $("#ovlLog");
  const log = initLog(ovlLog, $("#log"), $("#logLang") as HTMLSelectElement);
  let logOpener: HTMLElement | null = null;
  $("#tLog").addEventListener("click", (e) => {
    logOpener = e.currentTarget as HTMLElement;
    ovlLog.classList.add("on");
    log.render(true);
    ovlLog.querySelector<HTMLElement>("[data-close]")?.focus({ preventScroll: true });
  });
  const closeLog = () => {
    if (!ovlLog.classList.contains("on")) return;
    ovlLog.classList.remove("on");
    logOpener?.focus({ preventScroll: true });
  };
  ovlLog.querySelector("[data-close]")?.addEventListener("click", closeLog);
  ovlLog.addEventListener("click", (e) => {
    if (e.target === ovlLog) closeLog();
  });

  const renderQueue = initQueue($("#queue"), conn);
  const renderPanel = initPanel($("#cpanel"), stage, conn);
  const voice = initVoice(
    { ptt: $("#ptt") as HTMLButtonElement, line: $("#vline"), sub: $("#vsub"), toggle: $("#vtoggle") as HTMLButtonElement, more: $("#vmore") },
    conn,
  );
  const audio = initAudio($("#ttsBadge"), $("#ttsAudio") as HTMLAudioElement, backend);

  function closePops() {
    layersPop.classList.remove("on");
    tLayers.setAttribute("aria-expanded", "false");
    voice.close();
    $("#vtoggle").setAttribute("aria-expanded", "false");
    setSettings(false);
  }

  // header
  const kImm = $("#kImm"), kDel = $("#kDel"), kUnk = $("#kUnk"), kExt = $("#kExt"), simChip = $("#simChip"), siteName = $("#siteName");
  const renderHeader = () => {
    const s = store.snap;
    if (!s) return;
    const live = s.victims.filter((v) => v.status !== "extracted" && v.status !== "dismissed");
    const cnt = (k: string) => String(live.filter((v) => store.triageOf(v) === k).length);
    kImm.textContent = cnt("immediate");
    kDel.textContent = cnt("delayed");
    kUnk.textContent = String(live.filter((v) => store.triageOf(v) === "unknown" || store.triageOf(v) === "expectant").length);
    kExt.textContent = String(s.victims.filter((v) => v.status === "extracted").length);
    siteName.textContent = `${SITE.name} · ${s.mission.id}`;
    const sim = s.sim;
    const parts = [`SIM ${sim.status}`];
    if (sim.elapsed_s != null) parts.push(`${Math.round(sim.elapsed_s)}/${sim.requested_s ?? "—"} s`);
    simChip.textContent = parts.join(" · ");
    simChip.title = sim.error
      ? `Simulation error: ${sim.error}`
      : `Simulation ${sim.status}${sim.env_id ? ` · ${sim.env_id}` : ""}${sim.credits_est != null ? ` · ~${sim.credits_est.toFixed(2)} credits` : ""}`;
    simChip.dataset.state = sim.status;
  };
  const stT = $("#stT"), stSince = $("#stSince");
  const tickClocks = () => {
    const s = store.snap;
    if (!s) return;
    const t = store.missionT();
    stT.textContent = fmtMs(t);
    stSince.textContent = fmtHms((s.mission.since_collapse_s ?? 0) + (t - s.t));
  };
  window.setInterval(tickClocks, 250);

  store.subscribe(() => {
    tickClocks();
    renderHeader();
    renderQueue();
    renderPanel();
    cams.render();
    voice.render();
    audio.render();
    log.render();
  });
  voice.render();
  // Events are for toasts only (PROTOCOL): surface errors on the voice strip.
  store.onEvent((e) => {
    if (e.name === "error" || e.name === "sim.error") {
      const d = e.data ?? {};
      const why = String(d.reason ?? d.error ?? d.message ?? "error");
      voice.notice(e.name === "sim.error" ? `Simulation: ${why}` : `Backend: ${why}`);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closePops();
    closeLog();
    cams.setFocus(null);
    if (store.local.selected) {
      store.select(null);
      ($("#map") as HTMLElement).focus?.();
    }
  });

  // Read-only hooks for the Playwright tests (no mission logic here).
  (window as unknown as { __skypaw: unknown }).__skypaw = {
    snap: () => store.snap,
    mapStats: () => map.stats,
    project: (kind: "victim" | "drone" | "go2", id?: string) => map.project(kind, id),
    voice: () => voice.stats(),
    audioArmed: () => audio.armed(),
  };

  conn.connect();
}

const root = document.getElementById("app");
if (root) boot(root);
