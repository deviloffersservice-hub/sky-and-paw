/**
 * Camera tiles (bottom-left): "Drone sees" and "Go2 sees" as <img> from
 * `/frames/<frame_id>.jpg`, refreshed when the snapshot's frame_id changes.
 * Tap / Enter on a tile → focus mode (feed fills the stage, map becomes a 300×200
 * minimap); again, ⌖ or Esc → back.
 */
import { type Backend, httpUrl } from "./backend";
import { store } from "./state";

type CamKey = "drone" | "go2";

interface Tile {
  el: HTMLElement;
  img: HTMLImageElement;
  hud: HTMLElement;
  foot: HTMLElement;
  frame: string | null;
}

export function initCams(root: HTMLElement, stage: HTMLElement, backend: Backend) {
  const tiles = {} as Record<CamKey, Tile>;
  const make = (key: CamKey, label: string) => {
    const el = document.createElement("div");
    el.className = "cam";
    el.dataset.cam = key;
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-pressed", "false");
    el.setAttribute("aria-label", `${label}: focus this camera`);
    el.innerHTML = `<img alt="${label}" decoding="async"><span class="hud"></span><span class="hud foot"></span><span class="lbl">${label}</span>`;
    root.append(el);
    const img = el.querySelector("img") as HTMLImageElement;
    img.addEventListener("error", () => el.classList.add("nofeed"));
    img.addEventListener("load", () => el.classList.remove("nofeed"));
    tiles[key] = { el, img, hud: el.querySelector(".hud") as HTMLElement, foot: el.querySelector(".foot") as HTMLElement, frame: null };
  };
  make("drone", "Drone sees");
  make("go2", "Go2 sees");

  const setFocus = (key: CamKey | null) => {
    (Object.keys(tiles) as CamKey[]).forEach((k) => {
      const on = k === key;
      tiles[k].el.classList.toggle("focus", on);
      tiles[k].el.setAttribute("aria-pressed", String(on));
    });
    stage.classList.toggle("focus", key != null);
    window.dispatchEvent(new Event("resize"));
  };
  const toggle = (el: HTMLElement) => {
    const key = el.dataset.cam as CamKey;
    setFocus(el.classList.contains("focus") ? null : key);
  };
  root.addEventListener("click", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>(".cam");
    if (el) toggle(el);
  });
  root.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const el = (e.target as HTMLElement).closest<HTMLElement>(".cam");
    if (!el) return;
    e.preventDefault();
    toggle(el);
  });

  const render = () => {
    const s = store.snap;
    if (!s) return;
    const d = s.robots.drone;
    const g = s.robots.go2;
    for (const key of ["drone", "go2"] as CamKey[]) {
      const tile = tiles[key];
      const frame = s.robots[key].frame_id;
      if (frame !== tile.frame) {
        tile.frame = frame;
        if (frame) {
          tile.img.src = httpUrl(backend, `/frames/${encodeURIComponent(frame)}.jpg`);
          tile.el.classList.remove("nofeed");
        } else {
          tile.img.removeAttribute("src");
          tile.el.classList.add("nofeed");
        }
      }
    }
    const age = (a: number | null) => (a == null ? "—" : `${a.toFixed(1)} s`);
    tiles.drone.hud.textContent = `RGB · alt ${d.pos ? d.pos[2].toFixed(1) : "—"} m · ${d.mode}`;
    tiles.drone.foot.textContent = `${d.frame_id ?? "no frame yet"} · pose ${age(d.pose_age_s)}`;
    const hdg = g.yaw == null ? "—" : `${((Math.round(90 - (g.yaw * 180) / Math.PI) % 360) + 360) % 360}°`;
    tiles.go2.hud.textContent = `FRONT · hdg ${hdg} · ${g.mode}`;
    tiles.go2.foot.textContent = `${g.frame_id ?? "no frame yet"} · pose ${age(g.pose_age_s)}`;
  };

  return { render, setFocus, focused: () => stage.classList.contains("focus") };
}
