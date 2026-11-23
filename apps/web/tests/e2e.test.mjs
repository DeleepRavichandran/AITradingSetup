/* Browser tests for the Command Deck (Vega) and the Strategy Monitor.
   Runs the production build (`npm run build` first) with `next start`, mocks every /api call in the browser
   and fakes SpeechRecognition, so no back end, microphone or speech service is needed.
   Run: node --test tests/e2e.test.mjs
   Coverage note: this drives Playwright's Chromium. Branded Google Chrome and Microsoft Edge are not
   installed here, and their real speech services (which need the internet) are not exercised. */
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { chromium } from "playwright";
import { fixtures, resolve, SOURCE } from "./fixtures.mjs";

const require = createRequire(import.meta.url);
const AXE = require.resolve("axe-core/axe.min.js");
const PORT = 3999 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORT}`;
let server, browser;

/* Fake Web Speech API: window.__say(text, confidence) or window.__speechError(code) drive the last recogniser. */
const FAKE_SPEECH = `
  window.__rec = { started: 0, aborted: 0, last: null };
  class FakeRec { constructor(){ window.__rec.last = this; } start(){ window.__rec.started++; } stop(){ this.onend && this.onend(); }
    abort(){ window.__rec.aborted++; this.onend && this.onend(); } }
  window.SpeechRecognition = FakeRec; window.webkitSpeechRecognition = FakeRec;
  window.__say = (t, c) => { const r = window.__rec.last; r.onresult && r.onresult({ results: [[{ transcript: t, confidence: c }]] }); r.onend && r.onend(); };
  window.__speechError = (e) => { const r = window.__rec.last; r.onerror && r.onerror({ error: e }); r.onend && r.onend(); };
  window.__tracks = [];
  const gum = navigator.mediaDevices && navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  if (gum) navigator.mediaDevices.getUserMedia = async (c) => { const s = await gum(c); s.getTracks().forEach((t) => window.__tracks.push(t)); return s; };
  try { localStorage.setItem("nseql_voice_consent", "yes"); } catch {}
`;
const DENY = `
  const q = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (d) => (d && (d.name === "microphone" || d.name === "camera")) ? Promise.resolve({ state: "denied", onchange: null }) : q(d);
  navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("denied", "NotAllowedError"));
`;

async function open(path, { deny = false, consent = true, viewport = { width: 1280, height: 900 }, colorScheme = "light" } = {}) {
  const state = { kill: false, posts: [] };
  const ctx = await browser.newContext({ viewport, colorScheme, permissions: deny ? [] : ["camera", "microphone"] });
  await ctx.addInitScript(FAKE_SPEECH + (deny ? DENY : "") + (consent ? "" : "try { localStorage.removeItem('nseql_voice_consent'); } catch {}"));
  const fx = fixtures(state);
  await ctx.route("**/api/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname.replace(/^\/api/, "");
    if (req.method() === "POST") {
      state.posts.push({ path: p, body: req.postDataJSON() });
      if (p === "/risk/kill") state.kill = true;
      return route.fulfill({ json: { ok: true, id: "job-1", answer: "fixture answer" } });
    }
    const h = resolve(fx, p);
    return h ? route.fulfill({ json: h() }) : route.fulfill({ status: 404, json: { detail: `no fixture for ${p}` } });
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(BASE + path);
  return { page, ctx, state, errors };
}
const type = async (page, text) => { await page.getByLabel("Type a command").fill(text); await page.getByLabel("Type a command").press("Enter"); };
const lastVega = (page) => page.locator('[data-testid="log"] li').last();

before(async () => {
  server = spawn(process.execPath, [require.resolve("next/dist/bin/next"), "start", "-p", String(PORT), "-H", "127.0.0.1"], { stdio: ["ignore", "pipe", "pipe"] });
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(BASE + "/deck")).ok) break; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] });
});
after(async () => { await browser?.close(); server?.kill(); });

describe("Command Deck safety", () => {
  test("typed kill command needs confirmation; Confirm then engages it", async () => {
    const { page, ctx, state, errors } = await open("/deck");
    await type(page, "engage kill switch");
    await page.getByTestId("confirm-box").waitFor();
    assert.equal(state.posts.filter((p) => p.path === "/risk/kill").length, 0, "nothing posted before confirmation");
    await page.getByTestId("confirm-yes").click();
    await page.getByTestId("kill-state").filter({ hasText: "engaged" }).waitFor();
    const k = state.posts.filter((p) => p.path === "/risk/kill");
    assert.equal(k.length, 1);
    assert.match(k[0].body.operator, /confirmed/);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("other words, a spoken cancel, or the 10 s timeout cancel a pending action", async () => {
    const { page, ctx, state } = await open("/deck");
    await type(page, "run replay");
    await page.getByTestId("confirm-box").waitFor();
    await type(page, "what's the news");
    await page.getByTestId("confirm-box").waitFor({ state: "detached" });
    await type(page, "engage kill switch");
    await page.getByTestId("confirm-box").waitFor();
    await type(page, "cancel");
    await page.getByTestId("confirm-box").waitFor({ state: "detached" });
    await type(page, "run research");
    await page.getByTestId("confirm-box").waitFor();
    await page.getByTestId("confirm-box").waitFor({ state: "detached", timeout: 12_000 });
    await type(page, "confirm");                                  // too late: nothing pending
    await lastVega(page).filter({ hasText: "nothing waiting" }).waitFor();
    assert.deepEqual(state.posts.filter((p) => p.path !== "/chat").map((p) => p.path), []);
    await ctx.close();
  });

  test("manual Emergency stop acts at once, without confirmation", async () => {
    const { page, ctx, state } = await open("/deck");
    await page.getByTestId("emergency-stop").click();
    await page.getByTestId("kill-state").filter({ hasText: "engaged" }).waitFor();
    assert.equal(state.posts.filter((p) => p.path === "/risk/kill").length, 1);
    assert.match(state.posts[0].body.operator, /emergency stop button/);
    assert.equal(await page.getByTestId("emergency-stop").isDisabled(), true);
    await ctx.close();
  });

  test("resets and orders are refused and post nothing", async () => {
    const { page, ctx, state } = await open("/deck");
    for (const t of ["reset the kill switch", "disable the kill switch", "resume trading", "buy reliance", "place an order"]) {
      await type(page, t);
      await lastVega(page).filter({ hasText: /can't reset|Real orders are disabled/ }).waitFor();
    }
    assert.deepEqual(state.posts, []);
    await ctx.close();
  });

  test("speech: low confidence, negation and questions never act; exact phrase asks for confirmation", async () => {
    const { page, ctx, state } = await open("/deck");
    const speak = async (t, c) => { const n = await page.evaluate(() => window.__rec.started); await page.getByTestId("talk").click();
      await page.waitForFunction((k) => window.__rec.started > k, n); await page.evaluate(([a, b]) => window.__say(a, b), [t, c]); };
    await speak("engage kill switch", 0.4);
    await lastVega(page).filter({ hasText: "not sure I heard" }).waitFor();
    await speak("don't stop trading", 0.95);
    await lastVega(page).filter({ hasText: "negation" }).waitFor();
    await speak("what is the kill switch", 0.95);
    assert.equal(await page.getByTestId("confirm-box").count(), 0);
    await speak("engage kill switch", 0.95);
    await page.getByTestId("confirm-box").waitFor();
    assert.equal(state.posts.filter((p) => p.path === "/risk/kill").length, 0);
    await ctx.close();
  });

  test("speech errors are explained", async () => {
    const { page, ctx } = await open("/deck");
    for (const [code, text] of [["not-allowed", "refused"], ["network", "unreachable"], ["audio-capture", "No microphone"]]) {
      const n = await page.evaluate(() => window.__rec.started);
      await page.getByTestId("talk").click();
      await page.waitForFunction((k) => window.__rec.started > k, n);           // listening: handlers are attached
      await page.evaluate((c) => window.__speechError(c), code);
      await lastVega(page).filter({ hasText: text }).waitFor();
    }
    await ctx.close();
  });

  test("rapid repeated commands run once", async () => {
    const { page, ctx, state } = await open("/deck");
    for (let i = 0; i < 5; i++) await type(page, "run replay");
    await page.getByTestId("confirm-box").waitFor();
    assert.equal(await page.locator('[data-testid="log"] li').filter({ hasText: "run replay" }).count(), 1);
    await page.getByTestId("confirm-yes").click();
    await page.getByTestId("confirm-yes").click({ timeout: 500 }).catch(() => {});   // gone after the first click
    await lastVega(page).filter({ hasText: "five-session replay" }).waitFor();
    assert.equal(state.posts.filter((p) => p.path === "/screening/replay").length, 1);
    await ctx.close();
  });

  test("denied microphone and camera: clear messages, typing still works", async () => {
    const { page, ctx } = await open("/deck", { deny: true });
    await page.getByTestId("mic-permission").filter({ hasText: "denied" }).waitFor();
    assert.equal(await page.getByTestId("talk").isDisabled(), true);
    await page.getByTestId("camera-permission").filter({ hasText: "denied" }).waitFor();
    await page.getByRole("button", { name: "Turn on" }).click();
    await page.getByRole("alert").filter({ hasText: "Camera access was refused" }).waitFor();
    await type(page, "status report");
    await lastVega(page).filter({ hasText: "Live trading is off" }).waitFor();
    await ctx.close();
  });

  test("voice asks for consent first, with a privacy notice", async () => {
    const { page, ctx } = await open("/deck", { consent: false });
    await page.getByTestId("talk").click();
    await page.getByRole("dialog").filter({ hasText: "audio is sent" }).waitFor();
    assert.equal(await page.evaluate(() => window.__rec.started), 0, "no recognition before consent");
    await page.getByRole("button", { name: "Allow voice" }).click();
    await page.getByTestId("talk").click();
    await page.waitForFunction(() => window.__rec.started === 1);
    await ctx.close();
  });

  test("leaving the page releases the camera and microphone", async () => {
    const { page, ctx } = await open("/deck");
    await page.getByRole("button", { name: "Turn on" }).click();
    await page.getByTestId("camera-video").waitFor();
    await page.getByTestId("talk").click();                       // mic level meter opens a stream
    await page.waitForFunction(() => window.__tracks.length >= 2);
    await page.getByRole("link", { name: "Command Center" }).click();  // client-side navigation unmounts the Vega page
    await page.waitForURL(BASE + "/");
    await page.waitForFunction(() => window.__tracks.every((t) => t.readyState === "ended"));
    assert.ok(await page.evaluate(() => window.__rec.aborted) >= 1, "speech recognition aborted");
    await ctx.close();
  });
});

describe("Accessibility and layout", () => {
  for (const path of ["/", "/today", "/hud", "/contest", "/deck", "/screening", "/universe", "/planner", "/funds"]) for (const colorScheme of ["light", "dark"]) {
    test(`${path} (${colorScheme}): no serious or critical axe violations`, async () => {
      const { page, ctx } = await open(path, { colorScheme });
      await page.waitForLoadState("networkidle");
      await page.addScriptTag({ path: AXE });
      const res = await page.evaluate(async () => (await window.axe.run(document, { resultTypes: ["violations"] })).violations
        .filter((v) => ["serious", "critical"].includes(v.impact)).map((v) => `${v.id}: ${v.nodes.length} × ${v.nodes[0]?.target}`));
      assert.deepEqual(res, []);
      await ctx.close();
    });
  }
  for (const path of ["/", "/today", "/deck", "/screening", "/universe", "/planner", "/funds"]) {
    for (const width of [360, 768, 1280]) {
      test(`${path} at ${width}px: no horizontal scroll, key controls visible`, async () => {
        const { page, ctx, errors } = await open(path, { viewport: { width, height: 800 } });
        await page.waitForLoadState("networkidle");
        const overflow = await page.evaluate(() => [document.documentElement, document.body, ...document.querySelectorAll("[class*=deck]")]
          .some((el) => el.scrollWidth > el.clientWidth + 1));
        assert.equal(overflow, false);
        if (path === "/deck") assert.ok(await page.getByTestId("emergency-stop").isVisible());
        else assert.ok(await page.getByTestId("strip-estop").isVisible(), "status-bar emergency stop reachable");
        assert.ok(await page.getByRole("heading", { level: 1 }).isVisible());
        assert.deepEqual(errors, []);
        await ctx.close();
      });
    }
  }

  test("overview and readiness are shown from the API, not invented", async () => {
    const d = await open("/deck");
    await d.page.getByTestId("readiness").filter({ hasText: "1 of 3 ready" }).waitFor();
    await d.page.getByTestId("readiness").filter({ hasText: "Research sandbox" }).waitFor();
    await d.page.getByText("3 accepted · 3 refused").waitFor();
    await d.ctx.close();
    const s = await open("/screening");
    await s.page.getByRole("heading", { name: "Loop overview" }).waitFor();
    await s.page.getByRole("heading", { name: "Module readiness" }).waitFor();
    await s.page.getByText("simulated fills on Replay of SYNTHETIC data; not real trading results").waitFor();
    await s.ctx.close();
  });

  test("data source is labelled on the Deck and the Strategy Monitor", async () => {
    const d = await open("/deck");
    await d.page.getByTestId("data-source").filter({ hasText: "Synthetic replay" }).waitFor();
    await d.ctx.close();
    const s = await open("/screening");
    await s.page.getByText("Synthetic data says nothing about real markets").waitFor();
    await s.ctx.close();
  });

  test("My trading speaks plainly: status light, money, holdings and trades as sentences", async () => {
    const { page, ctx, errors } = await open("/today");
    await page.getByTestId("bot-light").getByText("Bot is working").waitFor();
    await page.getByText("−₹180").first().waitFor();
    await page.getByRole("heading", { name: "What you own now (1)" }).waitFor();
    await page.getByText("2 shares · bought at ₹812.50 · now ₹815.10").waitFor();
    await page.getByText("loss ₹41").waitFor();
    for (const id of ["chart-curve", "chart-alloc", "chart-wins", "chart-family", "chart-hours", "chart-busy", "rca"]) await page.getByTestId(id).waitFor();
    await page.getByTestId("rca").getByText("stopped out by noise", { exact: true }).waitFor();
    await page.getByTestId("limited").getByText("Trading rules 1.1. Buys held back today: cool-down after a sale 12 · already holding that stock 30.").waitFor();
    await page.getByRole("img", { name: "1 winning and 1 losing exits today" }).waitFor();
    await page.getByTestId("chart-alloc").getByText("Free cash", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Mission control: core gauge, bot, money, breadth, contest and event stream from real API data", async () => {
    const { page, ctx, errors } = await open("/hud");
    await page.getByRole("img", { name: /Account value ₹99,820, -0\.18% today, 5% invested/ }).waitFor();
    await page.getByTestId("hud-bot").getByText("Working").waitFor();
    await page.getByTestId("hud-source").getByText("Live 1-minute candles (adapter not verified)").waitFor();
    await page.getByTestId("hud-breadth").getByText("12 up").waitFor();
    await page.getByTestId("hud-contest").getByText("The forward contest starts with live screening.").waitFor();
    await page.getByTestId("hud-stream").getByText("RBI keeps repo rate unchanged").waitFor();
    await page.getByTestId("hud-stream").getByText(/ITC: Lost 40.50 INR/).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Incumbent vs challenger: verdict with data source and versions, curves, costs, both-lost warning", async () => {
    const { page, ctx, errors } = await open("/contest");
    await page.getByTestId("contest-verdict").getByText("not a forward verdict").waitFor();
    await page.getByTestId("contest-source").getByText("SYNTHETIC replay (made-up prices; says nothing about real markets)").waitFor();
    await page.getByTestId("contest-verdict").getByText("orb30-breadth-1.0").waitFor();
    await page.getByTestId("contest-verdict").getByText("screening rules 1.2 (registry v2)").waitFor();
    await page.getByTestId("contest-verdict").getByText("2026-01-12 → 2026-01-14 (3)").waitFor();
    await page.getByTestId("both-lost").waitFor();
    for (const id of ["contest-equity", "contest-drawdown", "contest-table"]) await page.getByTestId(id).waitFor();
    await page.getByTestId("contest-table").getByText("−₹1,900").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Command Center shows API figures with provenance, never invented ones", async () => {
    const { page, ctx, errors } = await open("/");
    await page.getByTestId("lede").filter({ hasText: "342 configurations" }).waitFor();
    await page.getByTestId("lede").filter({ hasText: "synthetic replay (made-up prices)" }).waitFor();
    await page.getByText("−₹20,890").first().waitFor();                                 // 979,110 - 1,000,000, sign before the rupee
    assert.ok(await page.getByTestId("prov").count() >= 5, "every panel has an evidence footer");
    await page.getByRole("heading", { name: "Platform status" }).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("status-bar emergency stop acts at once from any page", async () => {
    const { page, ctx, state } = await open("/universe");
    await page.getByTestId("strip-estop").click();
    await page.waitForFunction(() => true);
    await page.getByTestId("strip-kill").filter({ hasText: "engaged" }).waitFor();
    const k = state.posts.filter((x) => x.path === "/risk/kill");
    assert.equal(k.length, 1);
    assert.match(k[0].body.operator, /status bar emergency stop/);
    await ctx.close();
  });

  test("Strategy Universe: map filters the server query; a row opens the dossier", async () => {
    const { page, ctx, errors } = await open("/universe");
    const seen = [];
    page.on("request", (r) => { if (r.url().includes("/api/universe/configs?")) seen.push(r.url()); });
    await page.getByRole("button", { name: /^rsi 5m:/ }).click();
    await page.waitForFunction(() => true);
    await new Promise((r) => setTimeout(r, 400));
    assert.ok(seen.some((u) => u.includes("family=rsi") && u.includes("timeframe=5m")), "map click becomes a server-side filter");
    await page.getByTestId("vtable").getByText("0123456789abcdef").click();
    await page.getByTestId("dossier").waitFor();
    await page.getByTestId("dossier").getByText("All evidence comes from SYNTHETIC data").waitFor();
    assert.ok(page.url().includes("config=0123456789abcdef"), "dossier is linkable");
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Vega copilot (Ctrl K) answers with evidence links on any page", async () => {
    const { page, ctx } = await open("/planner");
    await page.getByTestId("strip-estop").waitFor();                  // shell hydrated: its keyboard listener is attached
    for (let i = 0; i < 10 && !(await page.getByTestId("copilot").count()); i++) {
      await page.keyboard.press("Control+k");
      await page.waitForTimeout(300);
    }
    await page.getByTestId("copilot").waitFor();
    await page.getByRole("button", { name: "Which strategies have the strongest validated evidence?" }).click();
    await page.getByTestId("copilot-answer").filter({ hasText: "No screening configuration is qualified yet" }).waitFor();
    await page.getByTestId("copilot-answer").getByText("Did not read: live market prices").waitFor();
    await page.getByTestId("copilot-answer").getByRole("link", { name: /configuration 0123456789abcdef/ }).click();
    await page.waitForURL(/\/universe\?config=0123456789abcdef/);
    await ctx.close();
  });

  test("Vega page sends free-form questions to the grounded copilot", async () => {
    const { page, ctx, state } = await open("/deck");
    await type(page, "which strategies have the strongest validated evidence?");
    await lastVega(page).filter({ hasText: "No screening configuration is qualified yet" }).waitFor();
    await lastVega(page).getByRole("link", { name: /configuration 0123456789abcdef/ }).waitFor();
    assert.equal(state.posts.filter((x) => x.path === "/chat").length, 0);
    await ctx.close();
  });

  test("planner labels projections as assumptions; funds page says when no NAVs exist", async () => {
    const p = await open("/planner");
    await p.page.getByTestId("plan-result").getByText("₹27,416").waitFor();
    await p.page.getByText("Projections, not forecasts.").waitFor();
    await p.page.getByText("an illustration of compounding, not a forecast").waitFor();
    await p.ctx.close();
    const f = await open("/funds");
    await f.page.getByText("No NAV data imported yet").waitFor();
    assert.equal(await f.page.getByTestId("scheme").count(), 0);
    await f.ctx.close();
  });
});
