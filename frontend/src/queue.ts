/**
 * Decision queue (top-right): renders `snapshot.decisions` with the mockup's card markup
 * and sends the lead's choice.
 *
 * Plan cards (a decision with `plan`): the primary action sends `confirm`, an action with
 * id `cancel` sends `cancel`, both with `plan_id`. Any other action sends the generic
 * `decide` with `decision_id` and the action id.
 */
import { type Decision, esc, store } from "./state";
import type { Connection } from "./ws";

const KIND_LABEL: Record<string, string> = {
  plan: "Confirm robot plan",
  speak: "Go2 speaker",
  alert: "Alert",
  warn: "Role check",
};

/** The client message for an action on a decision card. */
export function messageFor(d: Decision, actionId: string) {
  const action = d.actions.find((a) => a.id === actionId);
  if (d.plan?.id) {
    if (actionId === "cancel") return { type: "cancel" as const, plan_id: d.plan.id };
    if (action?.primary || actionId === "go" || actionId === "confirm") return { type: "confirm" as const, plan_id: d.plan.id };
  }
  return { type: "decide" as const, decision_id: d.id, action: actionId };
}

function card(d: Decision, pending: boolean): string {
  const kindClass = d.kind === "warn" ? "warn" : d.kind === "speak" ? "speak" : d.kind === "alert" ? "cutoff" : d.kind;
  const risk = d.plan?.risk ? ` risk-${esc(d.plan.risk)}` : "";
  const src = d.plan?.source_text ? `<p class="dec-b dec-src">“${esc(d.plan.source_text)}” <span class="mono">${esc(d.plan.source_lang ?? "")}</span></p>` : "";
  return `<div class="dec k-${esc(kindClass)}${risk}${pending ? " pending" : ""}" data-id="${esc(d.id)}" role="group" aria-label="${esc(KIND_LABEL[d.kind] ?? d.kind)}: ${esc(d.title)}">
    <div class="dec-h"><span class="dec-k">${esc(KIND_LABEL[d.kind] ?? d.kind)}</span><time class="mono">${esc(store.clockFor(`dec:${d.id}`, d.created_t))}</time></div>
    <b class="dec-t">${esc(d.title)}</b><p class="dec-b">${esc(d.body ?? (d.plan?.steps ?? []).join(" · "))}</p>${src}
    <div class="dec-a">${d.actions
      .map((a, i) => `<button class="btn ${a.primary || (i === 0 && !d.actions.some((x) => x.primary)) ? "primary" : ""}" data-dec="${esc(a.id)}" ${pending ? "disabled" : ""}>${esc(a.label)}</button>`)
      .join("")}</div></div>`;
}

export function initQueue(root: HTMLElement, conn: Connection): () => void {
  root.addEventListener("click", (e) => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-dec]");
    if (!b || b.disabled) return;
    const id = b.closest<HTMLElement>("[data-id]")?.dataset.id;
    const d = store.snap?.decisions.find((x) => x.id === id);
    if (!d || !b.dataset.dec) return;
    if (conn.send(messageFor(d, b.dataset.dec))) {
      store.local.pendingDecisions.set(d.id, performance.now());
      store.changed();
    }
  });
  // Keyed render: a card's element is replaced only when its markup changes, so the entry
  // animation runs once per card and keyboard focus survives snapshots.
  const cards = new Map<string, { html: string; el: HTMLElement }>();
  const narrow = window.matchMedia("(max-width: 600px)");
  narrow.addEventListener("change", () => store.changed());
  let tail: { html: string; el: HTMLElement } | null = null;
  const fromHtml = (html: string) => {
    const holder = document.createElement("div");
    holder.innerHTML = html;
    return holder.firstElementChild as HTMLElement;
  };
  return () => {
    const s = store.snap;
    const wanted: HTMLElement[] = [];
    const seen = new Set<string>();
    if (s) {
      const q = s.decisions;
      // Three cards on a laptop (SPEC 5.1.3); one on a phone-width window so the map stays usable.
      const max = narrow.matches ? 1 : 3;
      for (const d of q.slice(0, max)) {
        const html = card(d, store.local.pendingDecisions.has(d.id));
        let entry = cards.get(d.id);
        if (!entry || entry.html !== html) {
          const el = fromHtml(html);
          const active = document.activeElement as HTMLElement | null;
          const refocus = entry && active && entry.el.contains(active) ? active.dataset.dec : null;
          if (entry?.el.isConnected) {
            el.style.animation = "none";
            entry.el.replaceWith(el);
          }
          entry = { html, el };
          cards.set(d.id, entry);
          if (refocus) el.querySelector<HTMLElement>(`[data-dec="${CSS.escape(refocus)}"]`)?.focus({ preventScroll: true });
        }
        seen.add(d.id);
        wanted.push(entry.el);
      }
      const tailHtml =
        q.length > max
          ? `<div class="qmore">+${q.length - max} more waiting</div>`
          : q.length === 0 && (s.sim.status !== "stopped" || s.mission.phase !== "idle")
            ? '<div class="qcalm">No decisions pending</div>'
            : "";
      if (tailHtml) {
        if (!tail || tail.html !== tailHtml) tail = { html: tailHtml, el: fromHtml(tailHtml) };
        wanted.push(tail.el);
      }
    }
    for (const id of [...cards.keys()]) if (!seen.has(id)) cards.delete(id);
    // Insert / remove only what changed (moving a node would restart its animation).
    wanted.forEach((el, i) => {
      if (root.children[i] !== el) root.insertBefore(el, root.children[i] ?? null);
    });
    while (root.children.length > wanted.length) root.lastElementChild?.remove();
  };
}
