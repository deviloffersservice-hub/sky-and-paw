/**
 * Context panel (slides in from the right, 372 px) for a survivor or a robot.
 * v1 shapes: Survivor (triage → `triage`, Send Go2 → `command navigate_go2`, Go2 speaker
 * phrases → `tts`, Dismiss → `dismiss`) and Drone / Go2 (mode, battery, pose age;
 * Drone: Survey / Hold / Land; Go2: Stop + speaker phrases). Teammate and hazard shapes
 * are out of v1.
 */
import { sensorList } from "./map";
import { esc, fmtPos, type Lang, LANGS, MODELS, PHRASES, store, TRIAGE, TRIAGE_KEYS, type Triage, type Victim } from "./state";
import type { Connection } from "./ws";

const VSTATUS: Record<string, string> = {
  located: "Located", enroute: "Go2 en route", arrived: "Go2 on site", contact: "Contact", extracted: "Extracted", dismissed: "Dismissed",
};

function triageBar(v: Victim): string {
  const cur = store.triageOf(v);
  const sugg = v.suggested_triage;
  return `<div class="triage" data-id="${esc(v.id)}" role="group" aria-label="Triage">${TRIAGE_KEYS.map(
    (k) =>
      `<button class="tbtn t-${k} ${cur === k ? "on" : ""} ${sugg === k && !store.triageSet(v) ? "sugg" : ""}" data-tri="${k}" aria-pressed="${cur === k && store.triageSet(v)}">${TRIAGE[k].label}${sugg === k ? "<small>suggested</small>" : ""}</button>`,
  ).join("")}</div>`;
}

function evidence(v: Victim): string {
  const ev = v.evidence;
  const src = v.source ?? {};
  const tag = (k: string) => (src[k] ? ` · ${esc(src[k])}` : "");
  const row = (label: string, val: string, ok: boolean, note: string) =>
    `<div class="ev ${ok ? "on" : "off"}"><span class="ev-k">${label}</span><span class="ev-v">${val}</span><span class="ev-n">${note}</span></div>`;
  const radar = ev.radar;
  const co2 = ev.co2;
  return `<div class="evidence">${row("RGB · YOLOv8n", ev.rgb != null ? "person " + ev.rgb.toFixed(2) : "no detection", ev.rgb != null, (ev.rgb != null ? "drone frame" : "occluded") + tag("rgb"))}${row(
    "Thermal",
    ev.thermal != null ? "+" + ev.thermal.toFixed(1) + " °C" : "none",
    ev.thermal != null,
    (ev.thermal != null && ev.thermal > 8 ? "too hot for a body" : ev.thermal != null ? "body range" : "—") + tag("thermal"),
  )}${row("UWB radar", radar ? radar.bpm.toFixed(0) + " bpm · " + esc(radar.snr) : "not scanned", !!radar, (radar ? "heartbeat" : "Go2 scans on arrival") + tag("radar"))}${row(
    "Acoustic",
    ev.acoustic ? "tapping" : "silent",
    !!ev.acoustic,
    "Go2 mic" + tag("acoustic"),
  )}${row("CO₂", co2 != null ? "+" + Math.max(0, co2 - 300) + " ppm" : "—", co2 != null, "void breathing" + tag("co2"))}</div>`;
}

function speaker(lang: Lang): string {
  return `<div class="sec-h">Speaker · ${MODELS.tts} <select class="sel" data-lang aria-label="Survivor language">${LANGS.map(
    (l) => `<option value="${l.code}" ${l.code === lang ? "selected" : ""}>${l.native}</option>`,
  ).join("")}</select></div><div class="phrases">${PHRASES.map(
    (p) => `<button class="btn" data-ph="${p.id}">${esc(p.label)}<small>${esc(p.text[lang])}</small></button>`,
  ).join("")}</div><div class="msgrow"><input data-relay placeholder="Relay your words → translate → speak" aria-label="Words for the Go2 to speak"><button class="btn" data-act="relay">Speak</button></div>`;
}

function victimPanel(v: Victim): string {
  const s = store.snap;
  const go2 = s?.robots.go2;
  const pendingNav = s?.decisions.some((d) => d.plan?.intent === "navigate_go2" && d.plan.target === v.id);
  const go2State =
    v.status === "contact" ? "contact made" : v.status === "arrived" || (go2?.target === v.id && go2.mode === "arrived") ? "on site" : go2?.target === v.id && go2.mode === "moving" ? "en route" : v.status === "enroute" ? "en route" : pendingNav ? "awaiting confirm" : "not sent";
  const go2Class = v.status === "contact" ? "done" : go2State === "not sent" ? "" : "active";
  const closed = v.status === "extracted" || v.status === "dismissed";
  const life = v.life_sign ? ` <span class="chip life">♥ ${v.evidence.radar ? v.evidence.radar.bpm.toFixed(0) + " bpm" : "life sign"}</span>` : "";
  return `<article class="panel-v" data-id="${esc(v.id)}"><header class="ph"><div><h2 class="vid" id="cpTitle">${esc(v.id)}</h2> <span class="chip">${esc(VSTATUS[v.status] ?? v.status)}</span>${life}</div><button class="btn tiny" data-act="close" aria-label="Close panel">✕</button></header>
    <p class="where"><span class="mono">${fmtPos(v.pos)}</span> · fused ${(v.fused * 100).toFixed(0)}%${v.frames?.length ? ` · ${v.frames.length} frame${v.frames.length > 1 ? "s" : ""}` : ""}</p>
    <div class="sec-h">Triage <small>system suggests · you decide</small></div>${triageBar(v)}
    <div class="sec-h">Needs</div><div class="needs"><div class="need ${go2Class}"><b>Go2</b><span>${go2State}</span></div></div>
    <div class="sec-h">Send</div><div class="sendrow"><button class="btn who r-bot" data-act="go2" ${closed || go2?.mode === "moving" || pendingNav ? "disabled" : ""}><b>Go2</b>send<small>confirm</small></button></div>
    ${speaker(store.local.survivorLang)}
    <details class="ev-d"><summary>Evidence · ${esc(sensorList(v))} · fused ${(v.fused * 100).toFixed(0)}%</summary>${evidence(v)}</details>
    <footer class="acts">${!closed ? '<button class="btn ghost" data-act="dismiss">Dismiss</button>' : ""}</footer></article>`;
}

function age(a: number | null | undefined): string {
  return a == null ? "—" : `${a.toFixed(1)} s`;
}

function botPanel(kind: "drone" | "go2"): string {
  const s = store.snap;
  if (!s) return "";
  if (kind === "drone") {
    const d = s.robots.drone;
    const stale = d.pose_age_s != null && d.pose_age_s > 2;
    return `<article class="panel-t"><header class="ph"><div><h2 class="vid" id="cpTitle">Drone</h2> <span class="chip">${MODELS.drone}</span></div><button class="btn tiny" data-act="close" aria-label="Close panel">✕</button></header>
      <div class="state ${stale ? "bad" : ""}"><b>${esc(d.mode)}</b><span>alt ${d.pos ? d.pos[2].toFixed(1) : "—"} m · battery ${d.battery != null ? d.battery.toFixed(0) + "%" : "—"} · pose age ${age(d.pose_age_s)} · mapped ${s.survey ? s.survey.pct_mapped.toFixed(0) : 0}%</span><span class="mono">${fmtPos(d.pos)}${s.survey ? ` · waypoint ${Math.min(s.survey.waypoint_idx + 1, s.survey.waypoints.length)}/${s.survey.waypoints.length} · ${s.survey.frames} frames` : ""}</span></div>
      <div class="acts"><button class="btn" data-cmd="aerial_survey">Survey</button><button class="btn" data-cmd="stop_all" data-target="drone">Hold</button><button class="btn" data-cmd="land">Land</button></div></article>`;
  }
  const g = s.robots.go2;
  const stale = g.pose_age_s != null && g.pose_age_s > 2;
  return `<article class="panel-t"><header class="ph"><div><h2 class="vid" id="cpTitle">Go2</h2> <span class="chip">${MODELS.go2}</span></div><button class="btn tiny" data-act="close" aria-label="Close panel">✕</button></header>
    <div class="state ${stale || g.mode === "blocked" ? "bad" : ""}"><b>${esc(g.mode)}${g.target ? " · " + esc(g.target) : ""}</b><span>battery ${g.battery != null ? g.battery.toFixed(0) + "%" : "—"} · pose age ${age(g.pose_age_s)} · payload speaker + radar + CO₂ · 0.6 m/s</span><span class="mono">${fmtPos(g.pos)}${g.path?.length ? ` · ${g.path.length} path points` : ""}</span></div>
    <div class="acts"><button class="btn danger" data-cmd="stop_all" data-target="go2">Stop</button></div>
    ${speaker(store.local.survivorLang)}</article>`;
}

export function initPanel(cp: HTMLElement, stage: HTMLElement, conn: Connection) {
  const command = (intent: string, target: string | null) => conn.send({ type: "command", intent, target, params: {} });
  cp.addEventListener("click", (e) => {
    const sel = store.local.selected;
    if (!sel) return;
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-act],[data-tri],[data-ph],[data-cmd]");
    if (!b || b.disabled) return;
    const lang = store.local.survivorLang;
    if (b.dataset.tri && sel.kind === "victim") {
      const category = b.dataset.tri as Triage;
      if (conn.send({ type: "triage", victim_id: sel.id, category })) {
        store.local.pendingTriage.set(sel.id, { category, at: performance.now() });
        store.changed();
      }
      return;
    }
    if (b.dataset.ph) {
      conn.send({ type: "tts", phrase_id: b.dataset.ph, lang });
      flash(b);
      return;
    }
    if (b.dataset.cmd) {
      command(b.dataset.cmd, b.dataset.target ?? null);
      flash(b);
      return;
    }
    const a = b.dataset.act;
    if (a === "close") store.select(null);
    else if (a === "go2" && sel.kind === "victim") {
      command("navigate_go2", sel.id);
      flash(b);
    } else if (a === "dismiss" && sel.kind === "victim") conn.send({ type: "dismiss", victim_id: sel.id });
    else if (a === "relay") {
      const inp = cp.querySelector<HTMLInputElement>("[data-relay]");
      if (inp?.value.trim()) {
        conn.send({ type: "tts", text: inp.value.trim(), lang });
        inp.value = "";
      }
    }
  });
  cp.addEventListener("change", (e) => {
    const s = (e.target as HTMLElement).closest<HTMLSelectElement>("[data-lang]");
    if (s) {
      store.local.survivorLang = s.value as Lang;
      store.changed();
    }
  });
  cp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.target as HTMLElement).matches("[data-relay]")) cp.querySelector<HTMLButtonElement>('[data-act="relay"]')?.click();
  });

  let lastHtml = "";
  let wasOpen = false;
  return () => {
    const sel = store.local.selected;
    let html = "";
    if (sel?.kind === "victim") {
      const v = store.victim(sel.id);
      html = v ? victimPanel(v) : "";
    } else if (sel) html = botPanel(sel.kind);
    const open = !!html;
    stage.classList.toggle("panel", open);
    cp.setAttribute("aria-hidden", String(!open));
    cp.inert = !open;
    if (html === lastHtml) return;
    // Keep focus, an open <details> and a half-typed relay across snapshot re-renders.
    const active = document.activeElement as HTMLElement | null;
    const inside = active && cp.contains(active);
    const focusSel = inside ? selectorFor(active) : null;
    const relay = cp.querySelector<HTMLInputElement>("[data-relay]")?.value ?? "";
    const detailsOpen = cp.querySelector("details")?.open ?? false;
    const sameEntity = cp.firstElementChild?.getAttribute("data-key") === keyOf();
    cp.innerHTML = html;
    cp.firstElementChild?.setAttribute("data-key", keyOf());
    lastHtml = html;
    if (sameEntity) {
      const r = cp.querySelector<HTMLInputElement>("[data-relay]");
      if (r) r.value = relay;
      const d = cp.querySelector("details");
      if (d) d.open = detailsOpen;
      if (focusSel) cp.querySelector<HTMLElement>(focusSel)?.focus({ preventScroll: true });
    } else if (open && !wasOpen) {
      cp.querySelector<HTMLElement>('[data-act="close"]')?.focus({ preventScroll: true });
    }
    wasOpen = open;
  };
}

function keyOf(): string {
  const sel = store.local.selected;
  return sel ? (sel.kind === "victim" ? `v:${sel.id}` : sel.kind) : "";
}

function selectorFor(el: HTMLElement): string | null {
  for (const attr of ["data-tri", "data-ph", "data-cmd", "data-act"]) {
    const v = el.getAttribute(attr);
    if (v != null) return `[${attr}="${CSS.escape(v)}"]${el.dataset.target ? `[data-target="${CSS.escape(el.dataset.target)}"]` : ""}`;
  }
  if (el.matches("[data-relay]")) return "[data-relay]";
  if (el.matches("[data-lang]")) return "[data-lang]";
  if (el.tagName === "SUMMARY") return "summary";
  return null;
}

function flash(b: HTMLElement): void {
  b.classList.add("on");
  window.setTimeout(() => b.classList.remove("on"), 600);
}

