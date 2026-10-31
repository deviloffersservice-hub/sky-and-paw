/**
 * The one WebSocket to the backend (`<api>/ws`, docs/PROTOCOL.md).
 *
 * Sends `hello` (role lead, token from localStorage.skypaw_token when present), feeds
 * `snapshot`s into the store and `event`s to listeners, reconnects with backoff.
 * Binary frames out are microphone PCM; binary frames in are ignored.
 */
import { type Backend, wsUrl } from "./backend";
import { type ServerEvent, type Snapshot, store } from "./state";

export const TOKEN_KEY = "skypaw_token";
export type ConnState = "connecting" | "connected" | "offline";

const MIN_RETRY_MS = 1000;
const MAX_RETRY_MS = 10_000;

export function readToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function storeToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage blocked: the token applies to nothing this load.
  }
}

export type ClientMessage =
  | { type: "hello"; role: "lead"; token?: string }
  | { type: "mic"; state: "start" | "stop"; lang: string }
  | { type: "text_command"; text: string; lang: string }
  | { type: "confirm"; plan_id: string }
  | { type: "cancel"; plan_id: string }
  | { type: "decide"; decision_id: string; action: string }
  | { type: "command"; intent: string; target: string | null; params: Record<string, unknown> }
  | { type: "triage"; victim_id: string; category: string }
  | { type: "dismiss"; victim_id: string }
  | { type: "tts"; phrase_id?: string; text?: string; lang: string }
  | { type: "ping" };

export class Connection {
  state: ConnState = "connecting";
  detail = "";
  private socket: WebSocket | null = null;
  private retryMs = MIN_RETRY_MS;
  private retryTimer = 0;
  private pingTimer = 0;
  private stateListeners = new Set<(s: ConnState, detail: string) => void>();

  constructor(private backend: Backend) {}

  onState(fn: (s: ConnState, detail: string) => void): void {
    this.stateListeners.add(fn);
    fn(this.state, this.detail);
  }

  private setState(s: ConnState, detail = ""): void {
    this.state = s;
    this.detail = detail;
    this.stateListeners.forEach((fn) => fn(s, detail));
  }

  get open(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): void {
    window.clearTimeout(this.retryTimer);
    this.setState("connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl(this.backend));
    } catch {
      this.setState("offline", "invalid backend URL");
      return;
    }
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.addEventListener("open", () => {
      const token = readToken();
      this.send(token ? { type: "hello", role: "lead", token } : { type: "hello", role: "lead" });
      window.clearInterval(this.pingTimer);
      this.pingTimer = window.setInterval(() => this.send({ type: "ping" }), 15_000);
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      let msg: { type?: unknown } | null = null;
      try {
        msg = JSON.parse(event.data) as { type?: unknown };
      } catch {
        return;
      }
      if (!msg || typeof msg !== "object") return;
      if (this.state !== "connected" && (msg.type === "hello" || msg.type === "snapshot" || msg.type === "pong")) {
        this.retryMs = MIN_RETRY_MS;
        this.setState("connected");
      }
      if (msg.type === "snapshot") store.setSnapshot(msg as Snapshot);
      else if (msg.type === "event") store.emitEvent(msg as ServerEvent);
    });
    socket.addEventListener("close", (event) => {
      window.clearInterval(this.pingTimer);
      if (this.socket !== socket) return;
      this.socket = null;
      const why = event.code === 1008 ? "refused (origin or token)" : "";
      this.setState("offline", `${why ? `${why} · ` : ""}retrying in ${Math.round(this.retryMs / 1000)} s`);
      this.retryTimer = window.setTimeout(() => this.connect(), this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, MAX_RETRY_MS);
    });
  }

  /** Sends a PROTOCOL client message; returns false when the socket is not open. */
  send(msg: ClientMessage): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(msg));
    return true;
  }

  /** Sends one binary microphone frame (int16 LE PCM). */
  sendBinary(data: ArrayBuffer): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(data);
    return true;
  }
}
