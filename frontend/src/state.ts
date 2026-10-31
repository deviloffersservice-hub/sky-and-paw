/**
 * Typed snapshot from docs/PROTOCOL.md and the console's store.
 *
 * The console renders only from the latest `snapshot`; local state is limited to
 * view concerns (selection, focus, layers, language) and optimistic highlights
 * (a decision the lead has acted on, a triage just clicked) that the next
 * snapshot either confirms or clears.
 */

export type Vec3 = [number, number, number];
export type Lang = "hi-IN" | "mr-IN" | "ta-IN" | "en-IN";
export type Triage = "immediate" | "delayed" | "unknown" | "expectant";

export interface Mission {
  id: string;
  phase: string;
  since_collapse_s: number;
}

export interface Sim {
  status: string;
  ready: boolean;
  env_id?: string | null;
  requested_s?: number | null;
  elapsed_s?: number | null;
  credits_est?: number | null;
  error?: string | null;
}

export interface DroneState {
  pos: Vec3 | null;
  yaw: number | null;
  mode: string;
  battery: number | null;
  pose_age_s: number | null;
  frame_id: string | null;
}

export interface Go2State {
  pos: Vec3 | null;
  yaw: number | null;
  mode: string;
  battery: number | null;
  pose_age_s: number | null;
  target: string | null;
  frame_id: string | null;
  path: Vec3[] | null;
}

export interface Survey {
  pct_mapped: number;
  frames: number;
  waypoint_idx: number;
  waypoints: Vec3[];
}

export interface Grid {
  x0: number;
  y0: number;
  cs: number;
  w: number;
  h: number;
  cls: string;
  seen: string;
}

export interface Evidence {
  rgb?: number | null;
  thermal?: number | null;
  radar?: { bpm: number; snr: string } | null;
  acoustic?: unknown;
  co2?: number | null;
}

export interface Victim {
  id: string;
  pos: Vec3 | null;
  status: string;
  fused: number;
  life_sign: boolean;
  evidence: Evidence;
  suggested_triage: Triage | null;
  triage: Triage | null;
  frames?: string[];
  source?: Record<string, string>;
}

export interface Plan {
  id: string;
  intent: string;
  target: string | null;
  steps?: string[];
  risk?: string;
  est_s?: number;
  source_text?: string;
  source_lang?: string;
  model?: string;
}

export interface DecisionAction {
  id: string;
  label: string;
  primary?: boolean;
}

export interface Decision {
  id: string;
  kind: string;
  title: string;
  body?: string;
  created_t: number;
  plan?: Plan | null;
  actions: DecisionAction[];
}

export interface Transcript {
  active: boolean;
  partial: string;
  final: string;
  lang: string | null;
  final_t?: number | null;
}

export interface TtsPlaying {
  text: string;
  lang: string;
  voice: string;
  url: string;
  started_t: number;
}

export interface LogEntry {
  t: number;
  src: string;
  key: string;
  text: Partial<Record<Lang, string>> | string;
}

export interface Snapshot {
  type: "snapshot";
  t: number;
  ts: string;
  mission: Mission;
  sim: Sim;
  robots: { drone: DroneState; go2: Go2State };
  survey: Survey | null;
  grid: Grid | null;
  victims: Victim[];
  decisions: Decision[];
  transcript: Transcript | null;
  tts: { playing: TtsPlaying | null } | null;
  log: LogEntry[];
  team?: { id: string; name: string; role: string; lang: string }[];
}

export interface ServerEvent {
  type: "event";
  name: string;
  data: Record<string, unknown>;
  t: number;
}

export type Selection = { kind: "victim"; id: string } | { kind: "drone" } | { kind: "go2" } | null;

export interface LocalState {
  selected: Selection;
  /** Decision ids acted on locally; cards stay dimmed until the snapshot drops them. */
  pendingDecisions: Map<string, number>;
  /** victim id -> triage the lead clicked, shown until the snapshot agrees or 3 s pass. */
  pendingTriage: Map<string, { category: Triage; at: number }>;
  survivorLang: Lang;
  logLang: Lang;
}

export const LANGS: { code: Lang; native: string }[] = [
  { code: "hi-IN", native: "हिन्दी" },
  { code: "mr-IN", native: "मराठी" },
  { code: "ta-IN", native: "தமிழ்" },
  { code: "en-IN", native: "English" },
];
export const langName = (code: string | null | undefined): string =>
  LANGS.find((l) => l.code === code)?.native ?? (code || "—");

export const TRIAGE: Record<Triage, { label: string; rank: number }> = {
  immediate: { label: "Immediate", rank: 0 },
  delayed: { label: "Delayed", rank: 1 },
  unknown: { label: "Unknown", rank: 2 },
  expectant: { label: "Expectant", rank: 3 },
};
export const TRIAGE_KEYS = Object.keys(TRIAGE) as Triage[];

export const MODELS = {
  sttRealtime: "saaras:v3-realtime",
  llm: "sarvam-105b",
  tts: "bulbul:v3",
  go2: "unitree/go2",
  drone: "skydio/skydio-x2",
};

export const SITE = { name: "Ghatkopar · Block C", desc: "Collapsed 3-floor residential · 18 × 12 m" };

/** The Go2 speaker phrases (ids are sent as `tts.phrase_id`). */
export const PHRASES: { id: string; label: string; text: Record<Lang, string> }[] = [
  { id: "p_help", label: "Help is coming", text: { "en-IN": "Help is on the way. Stay calm.", "hi-IN": "मदद आ रही है। शांत रहें।", "mr-IN": "मदत येत आहे. शांत राहा.", "ta-IN": "உதவி வருகிறது. அமைதியாக இருங்கள்." } },
  { id: "p_hear", label: "Can you hear me?", text: { "en-IN": "Can you hear me? Tap twice on something.", "hi-IN": "क्या आप मुझे सुन सकते हैं? दो बार थपथपाएँ।", "mr-IN": "तुम्हाला ऐकू येतंय का? दोनदा टकटक करा.", "ta-IN": "என் குரல் கேட்கிறதா? இரண்டு முறை தட்டுங்கள்." } },
  { id: "p_still", label: "Don't move", text: { "en-IN": "Don't move. Cover your mouth and nose.", "hi-IN": "हिलें नहीं। मुँह और नाक ढक लें।", "mr-IN": "हलू नका. तोंड आणि नाक झाका.", "ta-IN": "அசையாதீர்கள். வாய், மூக்கை மூடுங்கள்." } },
  { id: "p_medic", label: "Medic is close", text: { "en-IN": "A medic is two minutes away. Keep breathing slowly.", "hi-IN": "मेडिक दो मिनट दूर है। धीरे-धीरे साँस लेते रहें।", "mr-IN": "मेडिक दोन मिनिटांवर आहे. हळू श्वास घ्या.", "ta-IN": "மருத்துவர் இரண்டு நிமிடத்தில். மெதுவாக சுவாசியுங்கள்." } },
];

/** Sample lead commands from the mockup (sent as `text_command`). */
export const LEAD_SAMPLES: { t: string; l: Lang }[] = [
  { t: "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो", l: "hi-IN" },
  { t: "dog को V-01 के पास भेजो", l: "hi-IN" },
  { t: "Tell them a medic is close, in Marathi", l: "en-IN" },
];

/* ---------- store ---------- */

type Listener = () => void;

class Store {
  snap: Snapshot | null = null;
  /** performance.now() when the latest snapshot arrived. */
  receivedAt = 0;
  local: LocalState = {
    selected: null,
    pendingDecisions: new Map(),
    pendingTriage: new Map(),
    survivorLang: "mr-IN",
    logLang: "en-IN",
  };
  private listeners = new Set<Listener>();
  private eventListeners = new Set<(e: ServerEvent) => void>();
  private scheduled = false;

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onEvent(fn: (e: ServerEvent) => void): () => void {
    this.eventListeners.add(fn);
    return () => this.eventListeners.delete(fn);
  }

  setSnapshot(snap: Snapshot): void {
    this.snap = normalise(snap);
    this.receivedAt = performance.now();
    this.reconcile();
    this.changed();
  }

  emitEvent(e: ServerEvent): void {
    this.eventListeners.forEach((fn) => fn(e));
  }

  /** Coalesces renders to one per animation frame. */
  changed(): void {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(() => {
      this.scheduled = false;
      this.listeners.forEach((fn) => fn());
    });
  }

  /** Drops optimistic state the snapshot has caught up with. */
  private reconcile(): void {
    const snap = this.snap;
    if (!snap) return;
    const now = performance.now();
    const ids = new Set(snap.decisions.map((d) => d.id));
    for (const [id, at] of this.local.pendingDecisions) {
      if (!ids.has(id) || now - at > 5000) this.local.pendingDecisions.delete(id);
    }
    for (const [id, p] of this.local.pendingTriage) {
      const v = snap.victims.find((x) => x.id === id);
      if (!v || v.triage === p.category || now - p.at > 3000) this.local.pendingTriage.delete(id);
    }
    const sel = this.local.selected;
    if (sel?.kind === "victim" && !snap.victims.some((v) => v.id === sel.id)) this.local.selected = null;
  }

  select(sel: Selection): void {
    this.local.selected = sel;
    this.changed();
  }

  victim(id: string): Victim | undefined {
    return this.snap?.victims.find((v) => v.id === id);
  }

  /** Effective triage: optimistic click, then the lead's choice, then the suggestion. */
  triageOf(v: Victim): Triage {
    return this.local.pendingTriage.get(v.id)?.category ?? v.triage ?? v.suggested_triage ?? "unknown";
  }

  /** Whether the lead has set the triage (pins show `?` until then). */
  triageSet(v: Victim): boolean {
    return this.local.pendingTriage.has(v.id) || v.triage != null;
  }

  /** Mission time now, extrapolated from the last snapshot so clocks tick smoothly. */
  missionT(): number {
    if (!this.snap) return 0;
    return this.snap.t + Math.min(1, (performance.now() - this.receivedAt) / 1000);
  }

  private clocks = new Map<string, string>();

  /**
   * Wall-clock HH:MM:SS for a mission time `t`, derived from the snapshot's `ts` the first
   * time `key` is seen and then kept, so labels do not flicker as snapshots arrive.
   */
  clockFor(key: string, t: number): string {
    let c = this.clocks.get(key);
    if (!c) {
      c = this.clockAt(t);
      this.clocks.set(key, c);
      if (this.clocks.size > 500) this.clocks.delete(this.clocks.keys().next().value as string);
    }
    return c;
  }

  /** Wall-clock HH:MM:SS for a mission time `t`, derived from the snapshot's `ts`. */
  clockAt(t: number): string {
    const snap = this.snap;
    if (!snap) return "--:--:--";
    const base = Date.parse(snap.ts);
    if (Number.isNaN(base)) return fmtMs(t);
    return new Date(base - (snap.t - t) * 1000).toTimeString().slice(0, 8);
  }
}

/** Fills optional parts so renderers can rely on arrays and objects being present. */
function normalise(s: Snapshot): Snapshot {
  const robots = s.robots ?? ({} as Snapshot["robots"]);
  return {
    ...s,
    mission: s.mission ?? { id: "—", phase: "idle", since_collapse_s: 0 },
    sim: s.sim ?? { status: "stopped", ready: false },
    robots: {
      drone: robots.drone ?? { pos: null, yaw: null, mode: "landed", battery: null, pose_age_s: null, frame_id: null },
      go2: robots.go2 ?? { pos: null, yaw: null, mode: "idle", battery: null, pose_age_s: null, target: null, frame_id: null, path: null },
    },
    victims: Array.isArray(s.victims) ? s.victims.map((v) => ({ ...v, evidence: v.evidence ?? {} })) : [],
    decisions: Array.isArray(s.decisions) ? s.decisions.map((d) => ({ ...d, actions: d.actions ?? [] })) : [],
    log: Array.isArray(s.log) ? s.log : [],
  };
}

export const store = new Store();

/* ---------- helpers ---------- */

export function esc(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

export function fmtMs(t: number): string {
  const s = Math.max(0, Math.floor(t));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtHms(t: number): string {
  const s = Math.max(0, Math.floor(t));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtPos(p: Vec3 | null | undefined): string {
  return p ? `(${p[0].toFixed(1)}, ${p[1].toFixed(1)})` : "(—)";
}

export function logText(e: LogEntry, lang: Lang): string {
  if (typeof e.text === "string") return e.text;
  if (!e.text) return e.key;
  return e.text[lang] ?? e.text["en-IN"] ?? Object.values(e.text)[0] ?? e.key;
}

function b64(s: string): Uint8Array {
  try {
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return new Uint8Array(0);
  }
}

export interface DecodedGrid {
  x0: number;
  y0: number;
  cs: number;
  w: number;
  h: number;
  /** Per cell: 0 unmapped, 1 passable, 2 difficult, 3 blocked. Index = j * w + i (i along +x, j along +y). */
  cls: Uint8Array;
  /** Per cell: 1 when seen. */
  seen: Uint8Array;
}

let gridCache: { cls: string; seen: string; grid: DecodedGrid } | null = null;

/**
 * Decodes `grid.cls` (w*h bytes, row-major, row j at y = y0 + (j + 0.5) * cs) and `grid.seen`.
 * `seen` is a packed bitmask (ceil(w*h/8) bytes, MSB first, as numpy.packbits) or, when the
 * server sends one byte per cell (w*h bytes), a byte mask; both are accepted.
 */
export function decodeGrid(g: Grid | null | undefined): DecodedGrid | null {
  if (!g || !g.w || !g.h || typeof g.cls !== "string") return null;
  if (gridCache && gridCache.cls === g.cls && gridCache.seen === g.seen && gridCache.grid.w === g.w && gridCache.grid.h === g.h) {
    const c = gridCache.grid;
    c.x0 = g.x0;
    c.y0 = g.y0;
    c.cs = g.cs;
    return c;
  }
  const n = g.w * g.h;
  const cls = new Uint8Array(n);
  cls.set(b64(g.cls).subarray(0, n));
  const sraw = b64(g.seen ?? "");
  const seen = new Uint8Array(n);
  if (sraw.length >= n) {
    for (let i = 0; i < n; i++) seen[i] = sraw[i] ? 1 : 0;
  } else {
    for (let i = 0; i < n; i++) seen[i] = ((sraw[i >> 3] ?? 0) >> (7 - (i & 7))) & 1;
  }
  const grid: DecodedGrid = { x0: g.x0, y0: g.y0, cs: g.cs, w: g.w, h: g.h, cls, seen };
  gridCache = { cls: g.cls, seen: g.seen, grid };
  return grid;
}
