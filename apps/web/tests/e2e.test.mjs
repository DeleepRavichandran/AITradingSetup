/* Browser tests for the Command Deck (Vega) and the Strategy Monitor.
   Runs the production build (`npm run build` first) with `next start`, mocks every /api call in the browser
   and fakes SpeechRecognition, so no back end, microphone or speech service is needed.
   Run: node --test tests/e2e.test.mjs
   Coverage note: this drives Playwright's Chromium. Branded Google Chrome and Microsoft Edge are not
   installed here, and their real speech services (which need the internet) are not exercised. */
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
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

async function open(path, { deny = false, consent = true, viewport = { width: 1280, height: 900 }, colorScheme = "light", override = {} } = {}) {
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
    if (override[p] === "hang") return;                                         // never answers: the page must say "Connecting"
    if (override[p]) { const o = override[p](fx); return route.fulfill(o.status ? o : { json: o }); }
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
    await page.waitForURL(BASE + "/command");
    await page.waitForFunction(() => window.__tracks.every((t) => t.readyState === "ended"));
    assert.ok(await page.evaluate(() => window.__rec.aborted) >= 1, "speech recognition aborted");
    await ctx.close();
  });
});

describe("Accessibility and layout", () => {
  for (const path of ["/", "/radar", "/heatmaps", "/book", "/integrity", "/history", "/brain", "/today", "/hud", "/contest", "/deck", "/screening", "/universe", "/planner", "/funds"]) for (const colorScheme of ["light", "dark"]) {
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
  for (const path of ["/", "/radar", "/heatmaps", "/integrity", "/brain", "/book", "/today", "/deck", "/screening", "/universe", "/planner", "/funds"]) {
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
    await page.getByTestId("strip-data").waitFor();                               // feed verification lives in the status bar only
    await page.getByTestId("hud-breadth").getByText("12 up").waitFor();
    await page.getByTestId("hud-contest").getByText("The forward contest starts with live screening.").waitFor();
    await page.getByTestId("hud-news").getByText("RBI keeps repo rate unchanged").waitFor();
    await page.getByTestId("hud-stream").getByText(/ITC: Lost 40.50 INR/).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Mission Control: first screen answers value, P&L today, since inception, exposure and what happened", async () => {
    const { page, ctx, errors } = await open("/hud");
    await page.getByTestId("kpi-value").getByText("₹99,820").waitFor();
    await page.getByTestId("kpi-today").getByText("−₹180").waitFor();
    await page.getByTestId("kpi-since").getByText("−₹180").waitFor();
    await page.getByTestId("kpi-exposure").getByText("4.8%").waitFor();
    await page.getByTestId("hud-what").getByText(/44 eligible BUY signals, 3 accepted, 3 orders, 5 fills; the most common refusal was churn limits \(20\)/).waitFor();
    await page.getByTestId("hud-session").getByText("Warm-up history (not tradable)").waitFor();
    // trading events come before general headlines
    const tr = await page.getByTestId("hud-stream").boundingBox(), nw = await page.getByTestId("hud-news").boundingBox();
    assert.ok(tr && nw && tr.y < nw.y);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Drill-down: a session count opens the ledger with matching filters, and Back keeps them", async () => {
    const { page, ctx, errors } = await open("/hud");
    await page.getByRole("link", { name: /refused: churn limits: 20 - open the ledger/ }).click();
    await page.waitForURL(/\/screening\?/);
    const u = new URL(page.url());
    assert.equal(u.searchParams.get("day"), "2026-11-23");
    assert.equal(u.searchParams.get("status"), "rejected");
    assert.equal(u.searchParams.get("reason"), "churn limits");
    assert.equal(u.searchParams.get("decision"), "BUY");
    await page.getByTestId("ledger-filters").getByText("reason: churn limits").waitFor();
    await page.getByRole("link", { name: "Strategy Universe" }).click();
    await page.waitForURL(/\/universe/);
    await page.goBack();
    await page.waitForURL(/reason=churn/);
    await page.getByTestId("ledger-filters").getByText("session 2026-11-23").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Zero-trade session: one explained card with the last candle and an example decision", async () => {
    const zero = (fx) => { const t = fx["/today"](); return { ...t, market: { open: false, state: "closed for the day" }, charts: { ...t.charts, equity_curve: [], rca: { won: 0, lost: 0, by_cause: [], latest: [] } },
      session: { ...t.session, accepted: 0, orders: 0, fills: 0, buy_fills: 0, sell_fills: 0, last_processed_minute: "2026-11-23T15:30:00" } }; };
    const { page, ctx, errors } = await open("/hud", { override: { "/today": zero } });
    await page.getByTestId("hud-state").getByText("No trades this session").waitFor();
    await page.getByTestId("hud-no-trades").getByText(/last candle 15:30 IST: no trades\. 44 eligible BUY signals; none became a fill\. Most were refused by churn limits \(20\)/).waitFor();
    assert.equal(await page.getByTestId("hud-curve").count(), 0);                // no redundant empty chart
    await page.getByRole("link", { name: "Open an example decision in the ledger" }).click();
    await page.waitForURL(/status=rejected/);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Loading versus offline: Connecting until the API answers, Lab offline when it fails", async () => {
    const a = await open("/hud", { override: { "/today": "hang" } });
    await a.page.getByTestId("hud-state").getByText("Connecting").waitFor();
    await a.ctx.close();
    const b = await open("/today", { override: { "/today": "hang" } });
    await b.page.getByTestId("today-connecting").waitFor();
    await b.ctx.close();
    const c = await open("/hud", { override: { "/today": () => ({ status: 503, json: { detail: "down" } }) } });
    await c.page.getByTestId("hud-state").getByText("Lab offline").waitFor();
    await c.ctx.close();
  });

  test("Sidebar names and page headings are the same for every workspace", async () => {
    const { NAV: nav } = await import("../lib/nav.ts");
    const { page, ctx } = await open("/");
    for (const [, items] of nav) for (const [href, label] of items) {
      await page.goto(BASE + href);
      await page.getByRole("heading", { level: 1, name: label, exact: true }).waitFor({ timeout: 15000 });
    }
    await ctx.close();
  });

  test("Journey: open a dossier from a filtered heatmap, go to the ledger, return with filters intact", async () => {
    const { page, ctx, errors } = await open("/universe");
    const cell = page.locator(".matrix .cell").first();
    await cell.waitFor();
    await cell.click();
    await page.waitForURL(/family=.*timeframe=/);
    const filtered = new URL(page.url());
    await page.getByRole("button", { name: /Clear map filter/ }).waitFor();
    await page.locator(".vt-row, [role=row]").nth(1).click().catch(() => {});
    if (!(await page.getByTestId("dossier").count())) await page.goto(BASE + `/universe?${filtered.searchParams}&config=0123456789abcdef`);
    await page.getByTestId("dossier").waitFor();
    await page.getByRole("link", { name: "Open in ledger" }).click();
    await page.waitForURL(/\/screening\?.*config=/);
    await page.getByTestId("ledger-filters").getByText(/configuration/).waitFor();
    await page.goBack();
    await page.waitForURL(/\/universe/);
    const back = new URL(page.url());
    assert.equal(back.searchParams.get("family"), filtered.searchParams.get("family"));
    assert.equal(back.searchParams.get("timeframe"), filtered.searchParams.get("timeframe"));
    await page.getByRole("button", { name: /Clear map filter/ }).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Journey: trace a backtest result to its dataset and an individual replayed fill", async () => {
    const { page, ctx, errors } = await open("/backtest?run=R1");
    await page.getByTestId("run-context").getByText("synthetic_v1").waitFor();
    await page.getByTestId("run-context").getByText("2023-01-02 → 2023-03-31").waitFor();
    await page.getByTestId("research-trail").getByText(/run R1/).waitFor();
    assert.equal(await page.getByTestId("run-context").getByRole("link", { name: "synthetic_v1" }).getAttribute("href"), "/data?dataset=synthetic_v1");
    await page.getByText("AAA").first().click();
    await page.waitForURL(/\/replay\?run=R1&trade=0/);
    await page.getByText("Fills and charges").waitFor();
    await page.getByText("100.05").first().waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Journey: explain a zero-trade session from the ledger (My Trading)", async () => {
    const zero = (fx) => { const t = fx["/today"](); return { ...t, market: { open: false, state: "closed for the day" },
      session: { ...t.session, accepted: 0, orders: 0, fills: 0, last_processed_minute: "2026-11-23T15:30:00" } }; };
    const { page, ctx, errors } = await open("/today", { override: { "/today": zero } });
    await page.getByTestId("today-what").getByText(/No trades this session\..*Most were refused by churn limits \(20\)/).waitFor();
    await page.getByTestId("today-session").locator("summary").click();
    await page.getByRole("link", { name: /refused: size or liquidity: 15 - open the ledger/ }).click();
    await page.waitForURL(/reason=size\+or\+liquidity/);
    await page.getByTestId("ledger-filters").getByText("reason: size or liquidity").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Chart analyst: candles with indicators, trend, levels and the plain read", async () => {
    const { page, ctx, errors } = await open("/charts");
    await page.getByTestId("chart-read").getByText("Next resistance: round number at 1,380.00", { exact: false }).waitFor();
    await page.getByTestId("chart-trend").getByText("15m: up").waitFor();
    await page.getByRole("heading", { name: "Support and resistance" }).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Fundamental agent: market mood, proof status, stock evidence, missing inputs shown", async () => {
    const { page, ctx, errors } = await open("/fundamental");
    await page.getByTestId("fa-mood").getByText("fear", { exact: true }).waitFor();
    await page.getByTestId("fa-proof").getByText("news: not enough evidence").waitFor();
    await page.getByTestId("fa-detail").getByText("revenue +20% YoY", { exact: false }).waitFor();
    await page.getByTestId("fa-detail").getByText("AAA wins big order", { exact: false }).waitFor();
    await page.getByRole("cell", { name: "BBB" }).click();
    await page.getByTestId("fa-detail").getByText("no result for BBB published before", { exact: false }).waitFor();
    await page.getByTestId("fa-notes").getByText("India VIX not available", { exact: false }).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Daily review: plain findings first, failed step shown, other sessions selectable", async () => {
    const { page, ctx, errors } = await open("/review");
    await page.getByTestId("review-headline").getByText("The bot closed 363 round trips", { exact: false }).waitFor();
    await page.getByTestId("review-state").getByText("Ready").waitFor();
    await page.getByTestId("review-steps").getByText("RuntimeError: boom", { exact: false }).waitFor();
    await page.getByText("first 30 min (09:16-09:45)").first().waitFor();
    await page.getByTestId("review-next").getByText("No new entries before 09:46.").waitFor();
    await page.getByRole("heading", { name: "Versus benchmarks" }).waitFor();
    await page.getByTestId("review-ledger").getByText("it protected the account", { exact: false }).waitFor();
    await page.getByText("stop did its job: price kept falling after the 0.5% stop").waitFor();
    await page.getByRole("button", { name: /20 Nov/ }).click();
    await page.getByTestId("review-state").getByText("Not run yet").waitFor();
    await page.getByRole("button", { name: "Run this review" }).waitFor();
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
    await page.getByTestId("rb-mc").getByText("Sequence risk.").waitFor();
    await page.getByTestId("rb-wf").getByText("memory, not edge").waitFor();
    await page.getByTestId("rb-sv").getByText("no edge to test").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Command Center shows API figures with provenance, never invented ones", async () => {
    const { page, ctx, errors } = await open("/command");
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
    await page.getByTestId("vtable").getByTitle("0123456789abcdef", { exact: true }).click();
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

describe("VEGA: Mission Control, radar, dossier, portfolio and evidence integrity", () => {
  const V = JSON.parse(readFileSync(new URL("./vega-fixture.json", import.meta.url), "utf8"));

  test("Mission Control: money, radar, decision stream, limits and the integrity verdict all come from the API", async () => {
    const { page, ctx, errors } = await open("/");
    await page.getByRole("heading", { level: 1, name: "Mission Control" }).waitFor();
    await page.getByTestId("radar-table").locator("tbody tr").first().waitFor();
    assert.equal(await page.getByTestId("radar-table").locator("tbody tr").count(), Math.min(8, V.radar.rows.length));
    await page.getByTestId("decision-stream").locator("li").first().waitFor();
    await page.getByTestId("blocked").getByText(V.radar.limits.blocked[0]).waitFor();
    await page.getByTestId("risk-usage").getByText("New trades today").waitFor();
    await page.getByTestId("research-brief").getByText(V.integrity.verdict_plain).waitFor();
    await page.getByTestId("coverage").getByText(`${V.radar.coverage.symbols_with_data}/${V.radar.coverage.symbols_expected} with data`).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Radar: a row opens the decision dossier by keyboard, with gates and recorded reasons; Escape closes it", async () => {
    const { page, ctx, errors } = await open("/radar");
    const row = page.getByTestId("radar-full").locator("tbody tr", { hasText: V.dossier_symbol });
    await row.focus();
    await page.keyboard.press("Enter");
    const d = page.getByTestId("dossier");
    await d.getByRole("heading", { name: new RegExp(V.dossier_symbol) }).waitFor();
    await d.getByText("Portfolio accepts new buys").waitFor();
    await d.getByText("Positive expected edge after costs (allocator)").waitFor();
    await d.getByText(/no later outcome is used/).waitFor();
    await page.keyboard.press("Escape");
    await d.waitFor({ state: "hidden" });
    const before = await page.getByTestId("radar-full").locator("tbody tr").count();
    await page.getByRole("button", { name: /^Watching/ }).click();
    const n = V.radar.counts.WATCH;
    if (n === 0) await page.getByText("No stock matches this filter.").waitFor();
    else assert.equal(await page.getByTestId("radar-full").locator("tbody tr").count(), n);
    assert.equal(before, V.radar.rows.length);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Evidence Integrity: verdict, every hypothesis with its falsification rule, bias audit, gates, and an operator decision", async () => {
    const { page, ctx, errors, state } = await open("/integrity");
    await page.getByTestId("ei-verdict").getByText(V.integrity.verdict).waitFor();
    assert.equal(await page.getByTestId("ei-hypotheses").locator("tbody tr").count(), V.integrity.hypotheses.length);
    await page.getByTestId("ei-hypotheses").getByText("H-EDGE").waitFor();
    await page.getByTestId("ei-biases").getByText("Survivorship").waitFor();
    await page.getByTestId("ei-gates").getByText("operator approval bound to this exact candidate (fingerprint)").waitFor();
    await page.getByTestId("ei-suspend").getByText("S1").waitFor();
    const f = page.getByTestId("decision-form");
    assert.ok(await f.getByRole("button", { name: "Record decision" }).isDisabled());
    await f.getByLabel(/Reason/).fill("Not enough confirmatory sessions yet; keep collecting evidence");
    await f.getByRole("button", { name: "Record decision" }).click();
    await f.getByText(/Recorded in the ledger/).waitFor();
    const p = state.posts.find((x) => x.path === "/integrity/decision");
    assert.equal(p.body.decision, "reject");
    // a decision on the challenger names the exact candidate it was made on
    const fp = V.integrity.promotion.candidate_fingerprint;
    if (fp) { assert.equal(p.body.subject, `challenger ${fp.fingerprint.slice(0, 16)}`); assert.equal(p.body.candidate_fingerprint, fp.fingerprint); }
    await page.getByTestId("ei-states").getByText("deployed: no").waitFor();                       // eligible, approved, deployed kept apart
    if (fp) await page.getByTestId("ei-fingerprint").getByText(new RegExp(fp.fingerprint.slice(0, 16))).waitFor();
    assert.equal(await page.getByTestId("ei-protocols").locator("tbody tr").count(), V.integrity.research_protocols.length);
    await page.getByTestId("ei-anchors").getByText(V.integrity.ledger.anchors.ok ? "consistent" : "mismatch").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Paper Portfolio: reconciliation and benchmarks from the API", async () => {
    const { page, ctx, errors } = await open("/book");
    await page.getByTestId("reconciliation").getByText("Marked equity now").waitFor();
    await page.getByTestId("benchmarks").getByText("buy and hold watchlist").waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Brain Intelligence: gates, decision versus outcome, versioned knowledge, shadow outcomes, measures and what is not built", async () => {
    const { page, ctx, errors } = await open("/brain");
    await page.getByRole("heading", { level: 1, name: "Brain Intelligence" }).waitFor();
    await page.getByTestId("gate-summary").getByText("Familiar market conditions").waitFor();
    await page.getByTestId("quality-matrix").locator("div").first().waitFor();
    assert.equal(await page.getByTestId("knowledge").locator("tbody tr").count(), V.brain.knowledge.length);
    await page.getByTestId("knowledge").getByText("K-PATTERN-opening").waitFor();
    await page.getByTestId("shadow").getByText("no trade").waitFor();
    await page.getByTestId("brain-metrics").getByText("False positives removed by correction").waitFor();
    await page.getByTestId("not-implemented").getByText(/futures and options/).waitFor();
    assert.equal(await page.getByText(/intelligence score:/i).count(), 0);
    await page.getByTestId("shadow-b").getByText(V.brain.shadow_B.reading).waitFor();               // hypothetical forward test, not account results
    assert.equal(await page.getByTestId("research-questions").locator(":scope > li").count(), Math.min(8, V.brain.research_questions.length));
    assert.equal(await page.getByTestId("recommendations").locator("li").count(), V.brain.recommendations.length);
    assert.equal(await page.getByTestId("recommendations").getByText(/: switch/).count(), 0);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Chart Analyst: market structure with layers, every pattern labelled inferred, its research verdict, and order flow stated unavailable", async () => {
    const { page, ctx, errors } = await open("/charts");
    await page.getByTestId("ms-events").locator("tbody tr").first().waitFor();
    assert.equal(await page.getByTestId("ms-events").locator("tbody tr").count(), Math.min(40, V.structure.events.length));
    await page.getByTestId("ms-labels").getByText(/not institutional intent/).waitFor();
    await page.getByTestId("ms-unavailable").getByText(/cumulative volume delta/).waitFor();
    const tags = await page.getByTestId("ms-events").locator(".tag").allTextContents();
    const STATUSES = ["insufficient evidence", "rejected", "exploratory finding", "validation candidate", "holdout-supported finding"];
    assert.ok(tags.length > 0 && tags.every((t) => STATUSES.includes(t.trim())), tags.join(","));  // never "validated"
    await page.getByRole("checkbox", { name: "Value area" }).check();
    await page.getByRole("checkbox", { name: "Fair value gaps" }).uncheck();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Brain: market-structure concepts on trial with verdicts against the simpler rule and random timing", async () => {
    const { page, ctx, errors } = await open("/brain");
    await page.getByTestId("concepts").locator("tbody tr").first().waitFor();
    assert.equal(await page.getByTestId("concepts").locator("tbody tr").count(), V.concepts.results.filter((r) => r.horizon === 6).length);
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Heatmaps: market map tiles open the dossier, grids drill down, unavailable maps say why, provenance and evidence shown", async () => {
    const { page, ctx, errors } = await open("/heatmaps");
    await page.getByRole("heading", { level: 1, name: "Heatmap Intelligence" }).waitFor();
    await page.getByTestId("treemap").locator("button").first().waitFor();
    assert.equal(await page.getByTestId("treemap").locator("button").count(), V.heatmaps.market_cap.tiles.length);
    await page.getByTestId("hm-provenance").getByText(/Source: ledger 1-minute candles/).waitFor();
    await page.getByTestId("treemap").locator("button").first().click();
    await page.getByTestId("dossier").getByRole("heading").first().waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Order-book liquidity (history)" }).click();
    await page.getByTestId("hm-unavailable").getByText(/not market depth/).waitFor();
    assert.equal(await page.getByTestId("hm-grid").count(), 0);                  // nothing drawn for data we do not have
    await page.getByRole("button", { name: "Relative strength" }).click();
    await page.getByTestId("hm-grid").waitFor();
    await page.getByTestId("hm-features").getByText("rs_60").waitFor();
    await page.getByRole("button", { name: "ICT/SMC inferred liquidity zones" }).click();
    await page.getByTestId("hm-grid").locator("button.hm-r").first().click();
    await page.getByTestId("hm-levels").getByText(/not resting orders/).waitFor();
    assert.deepEqual(errors, []);
    await ctx.close();
  });

  test("Historical validation: segments with benchmarks, contaminated days separate, synthetic labelled, operator can start a run", async () => {
    const { page, ctx, errors, state } = await open("/history");
    await page.getByTestId("hv-state").getByText("done").waitFor();
    await page.getByTestId("hv-kind").getByText(/SYNTHETIC replay/).waitFor();
    await page.getByTestId("hv-segment").getByText("buy and hold watchlist").waitFor();
    assert.ok(await page.getByTestId("hv-readings").locator(".tag").count() >= 2);
    await page.getByTestId("hv-segment").getByText("B0 matched control").waitFor();
    for (const k of Object.keys(V.history.report.segments.holdout.ablation)) await page.getByTestId("hv-ablation").getByText(k, { exact: true }).waitFor();
    assert.equal(await page.getByTestId("hv-quality").locator("tbody tr").count(), Object.keys(V.history.report.variants.trade_quality).length);
    await page.getByRole("button", { name: /^contaminated/ }).click();
    await page.getByText("sessions the live rules were shaped on: shown, never counted as evidence").waitFor();
    await page.getByTestId("hv-run").click();
    await page.getByText(/Result:/).waitFor();
    assert.equal(state.posts.filter((x) => x.path === "/history/run").length, 1);
    assert.deepEqual(errors, []);
    await ctx.close();
  });
});
