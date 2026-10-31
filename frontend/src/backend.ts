/**
 * Where the backend lives.
 *
 * Order: `?api=<url>` in the page URL (also remembered in localStorage),
 * then localStorage.skypaw_api, then the page's own origin (when FastAPI
 * serves the console). `?api=` with an empty value forgets the stored URL.
 */

export const STORAGE_KEY = "skypaw_api";

export type BackendSource = "query" | "storage" | "same-origin";

export interface Backend {
  /** Origin plus optional path prefix, no trailing slash, e.g. https://x-7860.app.github.dev */
  base: string;
  source: BackendSource;
}

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function storeBackend(value: string | null): void {
  try {
    if (value) window.localStorage.setItem(STORAGE_KEY, value);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked (private mode, sandbox): the URL parameter still works for this load.
  }
}

/** Returns a clean http(s) base URL or null when the input is empty or not a usable URL. */
export function normaliseBackend(raw: string | null | undefined): string | null {
  const value = (raw ?? "").trim();
  if (!value) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  return `${url.origin}${url.pathname}`.replace(/\/+$/, "");
}

export function resolveBackend(location: Location = window.location): Backend {
  const params = new URLSearchParams(location.search);
  if (params.has("api")) {
    const raw = params.get("api");
    const fromQuery = normaliseBackend(raw);
    if (fromQuery) {
      storeBackend(fromQuery);
      return { base: fromQuery, source: "query" };
    }
    if (!raw || !raw.trim()) storeBackend(null);
  }
  const stored = normaliseBackend(readStored());
  if (stored) return { base: stored, source: "storage" };
  return { base: location.origin, source: "same-origin" };
}

export function httpUrl(backend: Backend, path: string): string {
  return `${backend.base}${path.startsWith("/") ? path : `/${path}`}`;
}

export function wsUrl(backend: Backend, path = "/ws"): string {
  return httpUrl(backend, path).replace(/^http/, "ws");
}
