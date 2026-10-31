/**
 * 2D tactical map: the mockup's Canvas 2D renderer (SP.Map2D) driven by the live snapshot.
 *
 * World frame (PROTOCOL): x right, y up on screen (north up), z up out of the map.
 * The mockup drew its site frame (x, z ground) with z down the screen; world y = -site z,
 * so the picture is identical: screen = (cx + (x - vx) * ppm, cy - (y - vy) * ppm).
 * Tokens, fonts and visual language are the mockup's; layers cut from v1 (team, egress,
 * hazards) are not drawn.
 */
import { decodeGrid, store, TRIAGE, type Vec3, type Victim } from "./state";

export const PAL = {
  bg: "#0d131a", unknown: "#141b23", passable: "#1d3a2a", verified: "#245a3a", difficult: "#4a3a14", blocked: "#3a1a1a",
  accent: "#ff9f1c", ink: "#e6edf3", ink2: "#9fb0c0", muted: "#5e6f80", heat: "rgba(255,120,40,.5)", radar: "#ff5c5c",
  bad: "#ff5c5c", warn: "#ffd166", good: "#3ddc84", drone: "#5ab4ff", go2: "#c9d4e0", path: "#ff9f1c", victim: "#ff5c5c",
  candidate: "#9fb0c0", font: '"Barlow Condensed",sans-serif', mono: '"IBM Plex Mono",monospace',
};

/** Footprint (18 × 12 m) and staging area, as in the mockup, in world x/y. */
const FOOTPRINT: [number, number][] = [[-9, -6], [9, -6], [9, 6], [-9, 6], [-9, -6]];
const STAGING: [number, number] = [-12.5, -7.5];
const DEFAULT_VIEW = { x: -1, y: 0 };

export type LayerKey = "grid" | "thermal" | "radar" | "bots" | "fov" | "victims";
export const LAYERS: [LayerKey, string][] = [
  ["grid", "Passability"], ["thermal", "Thermal"], ["radar", "Radar"], ["bots", "Bot paths"], ["fov", "Camera FOV"], ["victims", "People"],
];

type Hit = { kind: "victim"; id: string } | { kind: "drone" } | { kind: "go2" };

export interface DrawStats {
  robots: number;
  victims: number;
  seenCells: number;
  goPath: number;
  surveyWaypoints: number;
}

const SENSOR_NAMES: Record<string, string> = { rgb: "RGB", thermal: "thermal", radar: "radar", acoustic: "acoustic", co2: "CO₂" };
export const sensorList = (v: Victim): string =>
  Object.entries(v.evidence ?? {})
    .filter(([, val]) => val != null && val !== false)
    .map(([k]) => SENSOR_NAMES[k] ?? k)
    .join(" + ") || "—";

export function triageColour(v: Victim): string {
  if (v.status === "dismissed") return PAL.muted;
  if (v.status === "extracted") return PAL.good;
  const t = store.triageOf(v);
  return t === "immediate" ? PAL.bad : t === "delayed" ? PAL.warn : t === "expectant" ? PAL.muted : PAL.candidate;
}

export class TacticalMap {
  readonly layers: Record<LayerKey, boolean> = { grid: true, thermal: true, radar: true, bots: true, fov: true, victims: true };
  stats: DrawStats = { robots: 0, victims: 0, seenCells: 0, goPath: 0, surveyWaypoints: 0 };
  private ctx: CanvasRenderingContext2D;
  private view = { x: DEFAULT_VIEW.x, y: DEFAULT_VIEW.y, ppm: 28, userZoom: false };
  private drag: { x: number; y: number; moved: boolean } | null = null;
  private hover: Hit | null = null;
  private running = true;

  constructor(private canvas: HTMLCanvasElement, private onEmptyClick: () => void) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas 2D unavailable");
    this.ctx = ctx;
    new ResizeObserver(() => this.resize()).observe(canvas);
    this.resize();
    this.bind();
    const loop = () => {
      if (this.running) this.draw();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  private dpr(): number {
    return this.canvas.width / (this.canvas.getBoundingClientRect().width || 1);
  }

  resize(): void {
    const r = this.canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.width = Math.max(1, Math.round(r.width * dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * dpr));
    if (!this.view.userZoom) this.view.ppm = Math.max(1, Math.min(this.canvas.width / 27, this.canvas.height / 18.5));
  }

  reset(): void {
    this.view.x = DEFAULT_VIEW.x;
    this.view.y = DEFAULT_VIEW.y;
    this.view.userZoom = false;
    this.resize();
  }

  setLayer(k: LayerKey, on: boolean): void {
    this.layers[k] = on;
  }

  /** World x/y → canvas pixels. */
  w2s(x: number, y: number): [number, number] {
    return [this.canvas.width / 2 + (x - this.view.x) * this.view.ppm, this.canvas.height / 2 - (y - this.view.y) * this.view.ppm];
  }

  /** CSS pixel position (relative to the canvas) of an entity, for tests and the pick UI. */
  project(kind: "victim" | "drone" | "go2", id?: string): { x: number; y: number } | null {
    const pos = this.posOf(kind === "victim" ? { kind, id: id ?? "" } : { kind });
    if (!pos) return null;
    const [px, py] = this.w2s(pos[0], pos[1]);
    const d = this.dpr();
    return { x: px / d, y: py / d };
  }

  private posOf(h: Hit): Vec3 | null {
    const s = store.snap;
    if (!s) return null;
    if (h.kind === "victim") return store.victim(h.id)?.pos ?? null;
    return s.robots[h.kind].pos;
  }

  private bind(): void {
    const c = this.canvas;
    c.addEventListener("pointerdown", (e) => {
      this.drag = { x: e.clientX, y: e.clientY, moved: false };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const dpr = this.dpr();
      if (this.drag) {
        const dx = e.clientX - this.drag.x;
        const dy = e.clientY - this.drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) this.drag.moved = true;
        if (this.drag.moved) {
          this.view.x -= (dx * dpr) / this.view.ppm;
          this.view.y += (dy * dpr) / this.view.ppm;
          this.drag.x = e.clientX;
          this.drag.y = e.clientY;
        }
      }
      this.hover = this.hit((e.clientX - r.left) * dpr, (e.clientY - r.top) * dpr);
      c.style.cursor = this.hover ? "pointer" : "crosshair";
    });
    c.addEventListener("pointerup", (e) => {
      if (this.drag && !this.drag.moved) {
        const r = c.getBoundingClientRect();
        const dpr = this.dpr();
        const h = this.hit((e.clientX - r.left) * dpr, (e.clientY - r.top) * dpr);
        if (h) store.select(h.kind === "victim" ? { kind: "victim", id: h.id } : { kind: h.kind });
        else this.onEmptyClick();
      }
      this.drag = null;
    });
    c.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        this.view.userZoom = true;
        this.view.ppm = Math.max(8, Math.min(90, this.view.ppm * (1 - e.deltaY * 0.001)));
      },
      { passive: false },
    );
    c.addEventListener("pointerleave", () => {
      this.hover = null;
    });
  }

  private hit(px: number, py: number): Hit | null {
    const s = store.snap;
    if (!s) return null;
    let best: Hit | null = null;
    let bd = 16 * this.dpr();
    const test = (h: Hit, pos: Vec3 | null) => {
      if (!pos) return;
      const [x, y] = this.w2s(pos[0], pos[1]);
      const d = Math.hypot(x - px, y - py);
      if (d < bd) {
        bd = d;
        best = h;
      }
    };
    if (this.layers.victims) s.victims.forEach((v) => test({ kind: "victim", id: v.id }, v.pos));
    if (this.layers.bots) {
      test({ kind: "drone" }, s.robots.drone.pos);
      test({ kind: "go2" }, s.robots.go2.pos);
    }
    return best;
  }

  private line(pts: (Vec3 | [number, number])[], color: string, w: number, dash?: number[]): void {
    if (!pts || pts.length < 2) return;
    const ctx = this.ctx;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const [x, y] = this.w2s(p[0], p[1]);
      if (i) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.setLineDash(dash ?? []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  draw(): void {
    const ctx = this.ctx;
    const pal = PAL;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const dpr = this.dpr();
    const f = (v: number) => v * dpr;
    const view = this.view;
    const L = this.layers;
    const s = store.snap;
    const stats: DrawStats = { robots: 0, victims: 0, seenCells: 0, goPath: 0, surveyWaypoints: 0 };
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = pal.bg;
    ctx.fillRect(0, 0, w, h);
    ctx.font = `600 ${f(11)}px ${pal.font}`;
    const sel = store.local.selected;

    // passability grid
    const g = s ? decodeGrid(s.grid) : null;
    if (g && L.grid) {
      const size = g.cs * view.ppm;
      for (let j = 0; j < g.h; j++) {
        for (let i = 0; i < g.w; i++) {
          const k = j * g.w + i;
          const x = g.x0 + i * g.cs;
          const yTop = g.y0 + (j + 1) * g.cs;
          const [px, py] = this.w2s(x, yTop);
          if (px > w || py > h || px + size < 0 || py + size < 0) continue;
          const seen = g.seen[k] === 1;
          const c = seen ? g.cls[k] : 0;
          if (seen) stats.seenCells++;
          ctx.fillStyle = c === 0 ? pal.unknown : c === 1 ? pal.passable : c === 2 ? pal.difficult : pal.blocked;
          ctx.fillRect(px, py, size + 0.5, size + 0.5);
        }
      }
    } else if (g) {
      for (let k = 0; k < g.seen.length; k++) stats.seenCells += g.seen[k] ?? 0;
    }

    this.line(FOOTPRINT, pal.accent, f(1.2), [f(6), f(4)]);
    {
      const [x, y] = this.w2s(STAGING[0], STAGING[1]);
      ctx.fillStyle = pal.ink2;
      ctx.fillText("STAGING W", x - f(28), y - f(26));
      ctx.strokeStyle = pal.ink2;
      ctx.setLineDash([f(3), f(3)]);
      ctx.strokeRect(x - f(40), y - f(20), f(80), f(60));
      ctx.setLineDash([]);
    }
    if (!s) {
      this.stats = stats;
      this.scale(f, h);
      return;
    }
    const t = store.missionT();
    const victims = s.victims.filter((v) => v.pos);
    const drone = s.robots.drone;
    const go2 = s.robots.go2;

    if (L.thermal) victims.forEach((v) => {
      const th = v.evidence.thermal;
      if (th == null || !v.pos) return;
      const [x, y] = this.w2s(v.pos[0], v.pos[1]);
      const r = f(10) + th * f(3);
      const gr = ctx.createRadialGradient(x, y, 0, x, y, r);
      gr.addColorStop(0, pal.heat);
      gr.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = gr;
      ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
    });
    if (L.radar) victims.forEach((v) => {
      const radar = v.evidence.radar;
      if (!radar || v.status === "extracted" || !v.pos) return;
      const [x, y] = this.w2s(v.pos[0], v.pos[1]);
      const ph = (t * (radar.bpm / 60)) % 1;
      ctx.strokeStyle = pal.radar;
      ctx.lineWidth = f(1.5);
      ctx.globalAlpha = 1 - ph;
      ctx.beginPath();
      ctx.arc(x, y, f(8) + ph * f(22), 0, Math.PI * 2);
      ctx.stroke();
      ctx.globalAlpha = 1;
    });

    // bot paths: survey pattern while planned / flying, Go2 path solid while moving, dashed while a plan waits
    if (L.bots) {
      const wps = s.survey?.waypoints ?? [];
      const pendingSurvey = s.decisions.some((d) => d.plan?.intent === "aerial_survey");
      if (wps.length > 1 && (pendingSurvey || drone.mode === "survey" || drone.mode === "takeoff")) {
        const idx = Math.max(0, Math.min(wps.length, s.survey?.waypoint_idx ?? 0));
        ctx.globalAlpha = 0.45;
        this.line(wps.slice(0, idx + 1), pal.drone, f(1.4));
        ctx.globalAlpha = 1;
        this.line(wps.slice(Math.max(0, idx - 1)), pal.drone, f(1), [f(3), f(5)]);
        ctx.fillStyle = pal.drone;
        wps.forEach((p, i) => {
          const [x, y] = this.w2s(p[0], p[1]);
          ctx.globalAlpha = i < idx ? 0.35 : 0.9;
          ctx.beginPath();
          ctx.arc(x, y, f(i === idx ? 3.5 : 2), 0, Math.PI * 2);
          ctx.fill();
        });
        ctx.globalAlpha = 1;
        stats.surveyWaypoints = wps.length;
      }
      if (go2.pos && go2.mode === "moving" && go2.path && go2.path.length) {
        this.line([go2.pos, ...go2.path], pal.path, f(2.2));
        stats.goPath = go2.path.length;
      }
      const pend = s.decisions.find((d) => d.plan?.intent === "navigate_go2" && d.plan.target);
      const tv = pend?.plan?.target ? store.victim(pend.plan.target) : undefined;
      if (pend && go2.pos && tv?.pos && !store.local.pendingDecisions.has(pend.id)) this.line([go2.pos, tv.pos], pal.path, f(2), [f(6), f(4)]);
    }

    // camera FOV
    if (L.fov) {
      if (drone.pos && drone.mode !== "landed") {
        const [x, y] = this.w2s(drone.pos[0], drone.pos[1]);
        const r = Math.max(0, drone.pos[2]) * Math.tan((36 * Math.PI) / 180) * view.ppm;
        ctx.fillStyle = pal.drone;
        ctx.globalAlpha = 0.1;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 0.5;
        ctx.strokeStyle = pal.drone;
        ctx.lineWidth = f(1);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      if (go2.pos) {
        const [gx, gy] = this.w2s(go2.pos[0], go2.pos[1]);
        const a = -(go2.yaw ?? 0);
        ctx.fillStyle = pal.go2;
        ctx.globalAlpha = 0.13;
        ctx.beginPath();
        ctx.moveTo(gx, gy);
        ctx.arc(gx, gy, 3.5 * view.ppm, a - 0.7, a + 0.7);
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    // people
    if (L.victims) victims.forEach((v) => {
      if (!v.pos) return;
      const [x, y] = this.w2s(v.pos[0], v.pos[1]);
      const col = triageColour(v);
      if (sel?.kind === "victim" && sel.id === v.id) {
        ctx.strokeStyle = pal.ink;
        ctx.lineWidth = f(2);
        ctx.beginPath();
        ctx.arc(x, y, f(15), 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.arc(x, y, f(7.5), 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = pal.bg;
      ctx.lineWidth = f(2);
      ctx.stroke();
      if (v.status === "extracted") {
        ctx.strokeStyle = pal.bg;
        ctx.lineWidth = f(2);
        ctx.beginPath();
        ctx.moveTo(x - f(3.5), y);
        ctx.lineTo(x - f(1), y + f(3));
        ctx.lineTo(x + f(4), y - f(3));
        ctx.stroke();
      }
      ctx.fillStyle = pal.ink;
      ctx.font = `700 ${f(11.5)}px ${pal.font}`;
      ctx.fillText(v.id + (v.status === "dismissed" ? " ✕" : ""), x + f(12), y - f(3));
      ctx.font = `600 ${f(9.5)}px ${pal.font}`;
      ctx.fillStyle = col;
      const label =
        v.status === "extracted"
          ? "EXTRACTED"
          : v.status === "dismissed"
            ? "DISMISSED"
            : TRIAGE[store.triageOf(v)].label.toUpperCase() + (store.triageSet(v) ? "" : "?") + (v.life_sign ? " ♥" : "");
      ctx.fillText(label, x + f(12), y + f(8));
      ctx.font = `600 ${f(11)}px ${pal.font}`;
      stats.victims++;
    });

    // robots
    if (L.bots) {
      if (drone.pos) {
        const [dx, dy] = this.w2s(drone.pos[0], drone.pos[1]);
        if (sel?.kind === "drone") this.ring(dx, dy, f);
        ctx.save();
        ctx.translate(dx, dy);
        ctx.rotate(Math.PI / 2 - (drone.yaw ?? 0));
        ctx.strokeStyle = pal.drone;
        ctx.lineWidth = f(2.5);
        ctx.beginPath();
        ctx.moveTo(-f(8), -f(8));
        ctx.lineTo(f(8), f(8));
        ctx.moveTo(f(8), -f(8));
        ctx.lineTo(-f(8), f(8));
        ctx.stroke();
        ctx.fillStyle = pal.drone;
        for (const o of [[-8, -8], [8, -8], [8, 8], [-8, 8]] as const) {
          ctx.beginPath();
          ctx.arc(f(o[0]), f(o[1]), f(3.5), 0, Math.PI * 2);
          ctx.fill();
        }
        // heading tick
        ctx.strokeStyle = pal.drone;
        ctx.lineWidth = f(1.5);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(0, -f(13));
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = pal.ink2;
        ctx.font = `500 ${f(9.5)}px ${pal.mono}`;
        ctx.fillText(`DRONE ${drone.pos[2].toFixed(1)}m`, dx + f(12), dy - f(8));
        stats.robots++;
      }
      if (go2.pos) {
        const [gx, gy] = this.w2s(go2.pos[0], go2.pos[1]);
        if (sel?.kind === "go2") this.ring(gx, gy, f);
        ctx.save();
        ctx.translate(gx, gy);
        ctx.rotate(Math.PI / 2 - (go2.yaw ?? 0));
        ctx.fillStyle = pal.go2;
        ctx.fillRect(-f(5), -f(9), f(10), f(18));
        ctx.fillStyle = pal.accent;
        ctx.beginPath();
        ctx.moveTo(0, -f(13));
        ctx.lineTo(f(4), -f(8));
        ctx.lineTo(-f(4), -f(8));
        ctx.fill();
        ctx.restore();
        ctx.fillStyle = pal.ink2;
        ctx.font = `500 ${f(9.5)}px ${pal.mono}`;
        ctx.fillText("GO2" + (go2.target ? " → " + go2.target : ""), gx + f(12), gy + f(4));
        ctx.font = `600 ${f(11)}px ${pal.font}`;
        stats.robots++;
      }
    }

    this.scale(f, h);

    // hover tooltip
    const hv = this.hover;
    if (hv) {
      const pos = this.posOf(hv);
      if (pos) {
        const [x, y] = this.w2s(pos[0], pos[1]);
        let txt = "";
        if (hv.kind === "victim") {
          const v = store.victim(hv.id);
          txt = v ? `${v.id} · ${TRIAGE[store.triageOf(v)].label} · ${sensorList(v)}` : "";
        } else if (hv.kind === "drone") txt = `Drone · ${drone.mode}`;
        else txt = `Go2 · ${go2.mode}`;
        if (txt) {
          ctx.font = `600 ${f(11)}px ${pal.font}`;
          const tw = ctx.measureText(txt).width + f(14);
          ctx.fillStyle = pal.ink;
          ctx.fillRect(x + f(14), y - f(30), tw, f(22));
          ctx.fillStyle = pal.bg;
          ctx.fillText(txt, x + f(21), y - f(15));
        }
      }
    }
    this.stats = stats;
  }

  private ring(x: number, y: number, f: (v: number) => number): void {
    const ctx = this.ctx;
    ctx.strokeStyle = PAL.ink;
    ctx.lineWidth = f(2);
    ctx.beginPath();
    ctx.arc(x, y, f(16), 0, Math.PI * 2);
    ctx.stroke();
  }

  private scale(f: (v: number) => number, h: number): void {
    const ctx = this.ctx;
    const px = 5 * this.view.ppm;
    ctx.strokeStyle = PAL.ink2;
    ctx.lineWidth = f(2);
    ctx.beginPath();
    ctx.moveTo(f(14), h - f(14));
    ctx.lineTo(f(14) + px, h - f(14));
    ctx.stroke();
    ctx.fillStyle = PAL.ink2;
    ctx.font = `500 ${f(10)}px ${PAL.mono}`;
    ctx.fillText("5 m", f(14), h - f(20));
  }
}
