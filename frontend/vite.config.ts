import { defineConfig, loadEnv, type Plugin } from "vite";

/**
 * VITE_BASE sets the public path the console is served from:
 *   "/"              when the FastAPI backend serves frontend/dist (default)
 *   "/sky-and-paw/"  for GitHub Pages (set by .github/workflows/pages.yml)
 */
function normaliseBase(raw: string | undefined): string {
  const value = (raw ?? "").trim();
  if (!value || value === "/") return "/";
  if (/^https?:\/\//.test(value)) return value.endsWith("/") ? value : `${value}/`;
  return `/${value.replace(/^\/+|\/+$/g, "")}/`;
}

const BACKEND = "http://localhost:7860";

/**
 * Writes the base the bundle was built with to dist/.vite-base, so `make run` / `make setup`
 * can tell a GitHub Pages build (/sky-and-paw/) from a backend build (/) and rebuild.
 */
function recordBase(base: string): Plugin {
  return {
    name: "skypaw-record-base",
    apply: "build",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: ".vite-base", source: `${base}\n` });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const base = normaliseBase(env.VITE_BASE);
  return {
    base,
    plugins: [recordBase(base)],
    build: {
      outDir: "dist",
      emptyOutDir: true,
      sourcemap: true,
      target: "es2022",
    },
    server: {
      port: 5173,
      // `npm run dev` talks to a backend started with `make run`.
      proxy: {
        "/healthz": BACKEND,
        "/version": BACKEND,
        "/ws": { target: BACKEND, ws: true },
      },
    },
  };
});
