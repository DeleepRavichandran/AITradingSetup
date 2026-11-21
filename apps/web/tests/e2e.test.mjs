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

const require = createRequire(import.meta.url);
const AXE = require.resolve("axe-core/axe.min.js");
const PORT = 3999 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORT}`;
let server, browser;

const SOURCE = { kind: "synthetic_replay", label: "Synthetic replay (made-up prices)", live: false, verified: false, synthetic: true,
  delay_seconds: null, freshness_seconds: null, stale: false, market_open: false, detail: "replayed from stored synthetic 1-minute bars" };
function fixtures(state) {
  return {
    "/system": () => ({ live_trading_enabled: false, kill_switch: { engaged: state.kill }, counts: { jobs_running: 0 }, code_version: "test",
      sandbox: { network_namespace: false }, llm: { configured: false } }),
    "/screening/status": () => ({ started: true, data_source: SOURCE, mode: "realistic", feed: "replay of fixture", registered: 342, symbols: 2,
      watermark: "2026-11-20T15:29:00+05:30", live: null, signals: 10, live_trading: false }),
    "/screening/portfolio": () => ({ shared_capital: true, mode: "realistic", equity_inr: 1000000, day_start_equity_inr: 1000000, cash_inr: 900000,
      reserved_inr: 0, open_positions: 1, working_orders: 0, gross_exposure_inr: 100000, halted_today: false, refused_signals: { symbol_cap: 3 },
      top_symbols: [{ symbol: "AAA", pct_equity: 0.1 }], rules: { max_positions: 50, per_trade_pct: 0.02, max_symbol_pct: 0.1, max_gross_pct: 1, daily_loss_pct: 0.02 },
      execution: { participation: 0.1, half_spread_bps: 5, entry_ttl_minutes: 5 }, note: "simulated" }),
    "/screening/summary": () => ({ horizons: { intraday: { evaluated_last_minute: 171, evaluated_ever: 171, open_positions: 1, pending_orders: 0 } },
      paper: { intraday: { filled: 4, realized_inr: 120, charges_inr: 40 } }, latency_ms_recent: { p99: 3.2 }, note: "simulated" }),
    "/screening/signals": () => ({ total: 1, rows: [{ bar_ts: "2026-11-20T10:00:00", symbol: "AAA", horizon: "intraday", decision: "BUY", reason: "close above SMA", price: 101.5, config_id: "abc123" }] }),
    "/screening/configs": () => ({ total: 1, rows: [{ id: "abc123", symbol: "AAA", timeframe: "5m", horizon: "intraday", family: "sma", params: { n: 20 }, stop_pct: 0.01 }] }),
    "/screening/orders": () => ({ total: 0, rows: [] }),
    "/screening/cycles": () => ({ total: 1, rows: [{ minute: "2026-11-20T10:00:00", status: "processed", symbols_with_data: 2, symbols_expected: 2, closed: ["1m"], eligible: 171, evaluated: 171, latency_ms: 2.1 }] }),
    "/readiness": () => ({ modules: [{ module: "API", status: "ready", detail: "up" }, { module: "Research sandbox", status: "blocked", detail: "no namespace" },
      { module: "Live order routing", status: "disabled", detail: "LIVE_TRADING_ENABLED=false" }], note: "derived at request time" }),
    "/screening/overview": () => ({ data_source: SOURCE, feed_health: null,
      configurations: { registered: 342, evaluated_last_cycle: 171, evaluated_ever: 342, qualified: 0, with_forward_evidence: 4, unverified: 342, qualified_rule: "at least 5 forward round trips" },
      cycles: { by_status: { processed: 10 }, missed_minutes: 0, overruns: 0, latency_ms: { p50: 1, p99: 3.2, max: 4 }, lag_s: null },
      signals: { by_decision: { BUY: 6, EXIT: 4 }, buy_allocation: { accepted: 3, deferred: 2, rejected: 1 }, refusal_reasons: { "max 50 positions": 1 } },
      capital: { equity_inr: 1000000, starting_capital_inr: 1000000, cash_inr: 900000, reserved_inr: 0, gross_exposure_inr: 100000, utilisation_pct: 10, open_positions: 1, halted_today: false },
      paper_performance: { equity_change_inr: 0, realised_after_charges_inr: 80, charges_paid_inr: 40, note: "simulated fills on Replay of SYNTHETIC data; not real trading results" },
      allocation: { method: "ranked" }, learning_brain: { verdict: "not benchmarked yet" },
      failures: { feed_status: null, feed_errors: {}, event_conflicts: 0, late_events: 0, portfolio_events: [], live: null }, recovery: { checkpoint: null, seconds: 0 }, readiness: [] }),
    "/news": () => ({ items: [{ title: "Test headline", source: "Fixture Wire", link: "https://example.invalid/a", published: "2026-11-20T09:00:00Z" }], errors: {} }),
  };
}

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
    const h = fx[p];
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
    await page.getByRole("link", { name: "Exit deck" }).click();  // client-side navigation unmounts the deck
    await page.waitForURL(BASE + "/");
    await page.waitForFunction(() => window.__tracks.every((t) => t.readyState === "ended"));
    assert.ok(await page.evaluate(() => window.__rec.aborted) >= 1, "speech recognition aborted");
    await ctx.close();
  });
});

describe("Accessibility and layout", () => {
  for (const path of ["/deck", "/screening"]) for (const colorScheme of ["light", "dark"]) {
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
  for (const path of ["/deck", "/screening"]) {
    for (const width of [360, 768, 1280]) {
      test(`${path} at ${width}px: no horizontal scroll, key controls visible`, async () => {
        const { page, ctx, errors } = await open(path, { viewport: { width, height: 800 } });
        await page.waitForLoadState("networkidle");
        const overflow = await page.evaluate(() => [document.documentElement, document.body, ...document.querySelectorAll("[class*=deck]")]
          .some((el) => el.scrollWidth > el.clientWidth + 1));
        assert.equal(overflow, false);
        if (path === "/deck") assert.ok(await page.getByTestId("emergency-stop").isVisible());
        else assert.ok(await page.getByText("Synthetic replay (made-up prices)").first().isVisible());
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
});
