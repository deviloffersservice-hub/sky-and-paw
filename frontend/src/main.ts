import "./styles.css";

import {
  type Backend,
  httpUrl,
  normaliseBackend,
  resolveBackend,
  storeBackend,
  wsUrl,
} from "./backend";

type ConnState = "connecting" | "connected" | "offline";

const TITLE = "Sky & Paw Ops";
const MAX_RETRY_MS = 10_000;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> & { className?: string } = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

function render(root: HTMLElement, backend: Backend) {
  const statusText = el("span", { className: "status-text" }, `${TITLE} · connecting…`);
  const status = el("div", { className: "status", role: "status" }, el("i"), statusText);
  status.setAttribute("aria-live", "polite");

  const input = el("input", {
    className: "sel mono",
    type: "url",
    name: "api",
    placeholder: "same origin",
    value: backend.source === "same-origin" ? "" : backend.base,
    spellcheck: false,
  });
  input.setAttribute("aria-label", "Backend URL");
  const form = el(
    "form",
    { className: "backend" },
    el("label", {}, "Backend"),
    input,
    el("button", { className: "btn", type: "submit" }, "Save"),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = normaliseBackend(input.value);
    if (input.value.trim() && !value) {
      input.setCustomValidity("Enter an http(s) URL, e.g. https://name-7860.app.github.dev");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    storeBackend(value);
    // Drop any ?api= so the saved value is what the next load uses.
    const next = new URL(window.location.href);
    next.searchParams.delete("api");
    window.location.assign(next.toString());
  });

  const header = el(
    "header",
    { className: "top" },
    el("div", { className: "brand" }, "SKY & PAW", el("small", {}, "Ops · simulation")),
    status,
    form,
  );

  const meta = el("dl", { className: "meta mono" });
  const addMeta = (k: string, v: string) => {
    const dd = el("dd", {}, v);
    meta.append(el("dt", {}, k), dd);
    return dd;
  };
  addMeta("backend", `${backend.base} (${backend.source})`);
  const versionDd = addMeta("version", "…");

  const stage = el(
    "main",
    { className: "stage" },
    el(
      "section",
      { className: "shell" },
      el("h1", {}, TITLE),
      el("p", { className: "dim" }, "Scaffold (T0). The tactical map, decision queue and voice strip arrive in later milestones."),
      meta,
    ),
  );

  root.replaceChildren(header, stage);

  return {
    setState(state: ConnState, detail = "") {
      document.body.dataset.conn = state;
      status.dataset.state = state;
      const label = state === "connecting" ? "connecting…" : state;
      statusText.textContent = `${TITLE} · ${label}${detail ? ` · ${detail}` : ""}`;
    },
    setVersion(version: string) {
      versionDd.textContent = version;
    },
  };
}

function connect(backend: Backend, ui: ReturnType<typeof render>) {
  let retryMs = 1000;

  const loadVersion = async () => {
    try {
      const resp = await fetch(httpUrl(backend, "/healthz"), { cache: "no-store" });
      const body = (await resp.json()) as { status?: string; version?: string };
      ui.setVersion(body.version ?? "unknown");
    } catch {
      ui.setVersion("unreachable");
    }
  };

  const open = () => {
    ui.setState("connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl(backend));
    } catch {
      ui.setState("offline", "invalid backend URL");
      return;
    }
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "hello", client: "ops" }));
    });
    socket.addEventListener("message", (event) => {
      let msg: unknown;
      try {
        msg = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (msg && typeof msg === "object" && (msg as { type?: unknown }).type === "hello") {
        retryMs = 1000;
        ui.setState("connected");
        void loadVersion();
      }
    });
    socket.addEventListener("close", () => {
      ui.setState("offline", `retrying in ${Math.round(retryMs / 1000)} s`);
      window.setTimeout(open, retryMs);
      retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
    });
  };

  open();
}

const root = document.getElementById("app");
if (root) {
  const backend = resolveBackend();
  const ui = render(root, backend);
  ui.setState("connecting");
  connect(backend, ui);
}
