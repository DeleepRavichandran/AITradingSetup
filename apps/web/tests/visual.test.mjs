/* Visual regression for the main workspaces, against mocked API data (tests/fixtures.mjs).
   Baselines live in tests/visual-baseline/ and are specific to the machine that made them (fonts and
   anti-aliasing differ between operating systems), so this suite is NOT part of CI.
     node --test tests/visual.test.mjs                  compare (fails above 0.5% changed pixels)
     UPDATE_VISUAL=1 node --test tests/visual.test.mjs  write new baselines after an intended change
   Web fonts are blocked during the run so text renders with the same local fallback every time;
   the live clock is masked. Diff images for failures are written to tests/visual-diff/. */
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { chromium } from "playwright";
import { fixtures, resolve } from "./fixtures.mjs";

const require = createRequire(import.meta.url);
const { PNG } = require("pngjs");
const pixelmatch = require("pixelmatch");
const DIR = path.join(path.dirname(new URL(import.meta.url).pathname), "visual-baseline");
const DIFF = path.join(path.dirname(DIR), "visual-diff");
const PORT = 4700 + Math.floor(Math.random() * 200);
const BASE = `http://127.0.0.1:${PORT}`;
const UPDATE = !!process.env.UPDATE_VISUAL;
const SHOTS = [["/", "desktop", 1280, 900], ["/", "mobile", 390, 844], ["/universe", "desktop", 1280, 900], ["/deck", "desktop", 1280, 900],
  ["/planner", "desktop", 1280, 900], ["/funds", "mobile", 390, 844]];
let server, browser;

before(async () => {
  server = spawn(process.execPath, [require.resolve("next/dist/bin/next"), "start", "-p", String(PORT), "-H", "127.0.0.1"], { stdio: "ignore" });
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(BASE + "/")).ok) break; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  browser = await chromium.launch();
  fs.mkdirSync(DIR, { recursive: true });
});
after(async () => { await browser?.close(); server?.kill(); });

for (const [route, size, w, h] of SHOTS) {
  const name = `${route === "/" ? "home" : route.slice(1)}-${size}`;
  test(`visual: ${name}`, async () => {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, colorScheme: "dark", reducedMotion: "reduce" });
    await ctx.addInitScript(() => { try { localStorage.setItem("nseql_voice_consent", "yes"); } catch { /* */ } });
    await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
    const fx = fixtures({ kill: false });
    await ctx.route("**/api/**", (r) => {
      const p = new URL(r.request().url()).pathname.replace(/^\/api/, "");
      const f = resolve(fx, p);
      return f ? r.fulfill({ json: f() }) : r.fulfill({ status: 404, json: { detail: "no fixture" } });
    });
    const page = await ctx.newPage();
    await page.goto(BASE + route, { waitUntil: "load" });
    await page.waitForTimeout(1500);
    const png = await page.screenshot({ mask: [page.locator("time")], animations: "disabled" });
    await ctx.close();
    const file = path.join(DIR, `${name}.png`);
    if (UPDATE || !fs.existsSync(file)) {
      fs.writeFileSync(file, png);
      return;
    }
    const a = PNG.sync.read(fs.readFileSync(file)), b = PNG.sync.read(png);
    assert.equal(`${b.width}x${b.height}`, `${a.width}x${a.height}`, "screenshot size changed");
    const out = new PNG({ width: a.width, height: a.height });
    const changed = pixelmatch(a.data, b.data, out.data, a.width, a.height, { threshold: 0.15 });
    const ratio = changed / (a.width * a.height);
    if (ratio > 0.005) {
      fs.mkdirSync(DIFF, { recursive: true });
      fs.writeFileSync(path.join(DIFF, `${name}.png`), PNG.sync.write(out));
    }
    assert.ok(ratio <= 0.005, `${(ratio * 100).toFixed(2)}% of pixels changed (diff in tests/visual-diff/${name}.png)`);
  });
}
