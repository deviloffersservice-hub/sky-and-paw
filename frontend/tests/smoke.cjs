#!/usr/bin/env node
/**
 * Smoke test: the running backend answers /healthz and serves the console shell.
 *
 *   SMOKE_URL=http://127.0.0.1:7860 SMOKE_SCREENSHOT=out.png node tests/smoke.cjs
 *
 * Checks: GET /healthz -> 200 {"status":"ok"}; GET / -> title "Sky & Paw Ops";
 * the shell's /ws hello round-trip turns the status to "connected".
 *
 * Playwright: uses the project's @playwright/test (CI installs its Chromium); if that
 * Chromium is not installed, falls back to a machine-wide `playwright` found on NODE_PATH,
 * so a dev box with browsers already installed needs no download. PLAYWRIGHT_MODULE
 * forces a specific module name or path.
 */
"use strict";

const path = require("node:path");

const BASE = (process.env.SMOKE_URL || "http://127.0.0.1:7860").replace(/\/+$/, "");
const SHOT = process.env.SMOKE_SCREENSHOT || "";
const TITLE = "Sky & Paw Ops";

function candidates() {
  const list = [];
  if (process.env.PLAYWRIGHT_MODULE) list.push(process.env.PLAYWRIGHT_MODULE);
  list.push("@playwright/test", "playwright");
  for (const dir of (process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean)) {
    list.push(path.join(dir, "playwright"));
  }
  return [...new Set(list)];
}

async function launchChromium() {
  const errors = [];
  for (const name of candidates()) {
    let mod;
    try {
      mod = require(name);
    } catch {
      errors.push(`${name}: not installed`);
      continue;
    }
    try {
      const browser = await mod.chromium.launch();
      return { browser, name };
    } catch (err) {
      errors.push(`${name}: ${String(err.message).split("\n")[0]}`);
    }
  }
  throw new Error(`No usable Playwright Chromium:\n  ${errors.join("\n  ")}`);
}

function check(cond, message) {
  if (!cond) throw new Error(`FAIL: ${message}`);
  console.log(`ok   ${message}`);
}

async function main() {
  const health = await fetch(`${BASE}/healthz`);
  const healthBody = await health.json();
  check(health.status === 200, `GET /healthz -> ${health.status}`);
  check(healthBody.status === "ok", `/healthz body ${JSON.stringify(healthBody)}`);

  const { browser, name } = await launchChromium();
  console.log(`info playwright module: ${name}, chromium ${browser.version()}`);
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    const consoleErrors = [];
    page.on("pageerror", (err) => consoleErrors.push(String(err)));
    const resp = await page.goto(`${BASE}/`, { waitUntil: "load" });
    check(resp && resp.status() === 200, `GET / -> ${resp && resp.status()}`);
    const title = await page.title();
    check(title === TITLE, `page title is "${title}"`);
    await page.waitForSelector('body[data-conn="connected"]', { timeout: 10_000 });
    const status = await page.textContent(".status-text");
    check(status === `${TITLE} · connected`, `status text "${status}" (WebSocket hello echoed)`);
    check(consoleErrors.length === 0, `no page errors ${JSON.stringify(consoleErrors)}`);
    if (SHOT) {
      await page.screenshot({ path: SHOT, fullPage: true });
      console.log(`info screenshot: ${SHOT}`);
    }
  } finally {
    await browser.close();
  }
  console.log("SMOKE PASS");
}

main().catch((err) => {
  console.error(String(err && err.stack ? err.stack : err));
  console.error("SMOKE FAIL");
  process.exit(1);
});
