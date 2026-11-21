"use client";
/* Command Deck: a voice-driven, full-screen view of the lab.
   Voice uses the browser's speech recognition (Chrome / Edge) and speech synthesis; the camera stays in the
   browser (frames are only compared locally for motion, never uploaded). Every action calls the same API as
   the rest of the dashboard. Real orders do not exist here: the assistant refuses to place them. */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import styles from "./deck.module.css";

type Line = { who: "you" | "vega"; text: string; at: string };
const NAME = "Vega";
const istNow = () => new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const inr = (x: number | null | undefined) => (x == null ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`);
const PAGES: [RegExp, string, string][] = [
  [/strategy monitor|screen/, "/screening", "Strategy Monitor"], [/learning|brain/, "/learning", "Learning Brain"],
  [/backtest/, "/backtest", "Backtesting Studio"], [/risk|kill switch page/, "/risk", "Risk Control Center"],
  [/paper/, "/paper", "Paper Trading Terminal"], [/command|home/, "/", "Command Center"],
];

/* ------------------------------------------------------------------ the reactor */
function Reactor({ level, mode }: { level: number; mode: "idle" | "listening" | "speaking" | "alert" }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const lv = useRef(level);
  lv.current = level;
  useEffect(() => {
    const c = ref.current!;
    const g = c.getContext("2d")!;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let t = 0;
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.42;
      const hue = mode === "alert" ? "255,77,94" : mode === "speaking" ? "255,181,71" : "95,227,255";
      const pulse = 1 + lv.current * 0.18;
      t += reduce ? 0 : 0.006 + lv.current * 0.04;
      const ring = (r: number, segs: number, gap: number, width: number, alpha: number, rot: number) => {
        g.lineWidth = width;
        g.strokeStyle = `rgba(${hue},${alpha})`;
        for (let i = 0; i < segs; i++) {
          const a0 = rot + (i / segs) * Math.PI * 2, a1 = a0 + (Math.PI * 2) / segs - gap;
          g.beginPath(); g.arc(cx, cy, r, a0, a1); g.stroke();
        }
      };
      ring(R * pulse, 3, 0.5, 1.5, 0.35, t * 0.6);
      ring(R * 0.86, 48, 0.07, 6, 0.55, -t);
      ring(R * 0.74, 6, 0.35, 2, 0.8, t * 1.7);
      ring(R * 0.62, 24, 0.12, 10, 0.25 + lv.current * 0.5, -t * 0.5);
      const core = g.createRadialGradient(cx, cy, 0, cx, cy, R * 0.48 * pulse);
      core.addColorStop(0, `rgba(${hue},0.95)`); core.addColorStop(0.35, `rgba(${hue},0.35)`); core.addColorStop(1, `rgba(${hue},0)`);
      g.fillStyle = core; g.beginPath(); g.arc(cx, cy, R * 0.5 * pulse, 0, Math.PI * 2); g.fill();
      for (let i = 0; i < 60; i++) {                                   // tick marks on the outer scale
        const a = (i / 60) * Math.PI * 2 + t * 0.15, r0 = R * 1.04, r1 = R * (i % 5 ? 1.08 : 1.13);
        g.strokeStyle = `rgba(${hue},${i % 5 ? 0.25 : 0.6})`; g.lineWidth = 1;
        g.beginPath(); g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0); g.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1); g.stroke();
      }
      raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [mode]);
  return <canvas ref={ref} className={styles.reactor} aria-hidden="true" />;
}

/* ------------------------------------------------------------------ camera */
function Camera({ onPresence }: { onPresence: (motion: number) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const [on, setOn] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [motion, setMotion] = useState(0);
  const stream = useRef<MediaStream | null>(null);
  useEffect(() => {
    if (!on) return;
    let timer: any, prev: Uint8ClampedArray | null = null;
    const small = document.createElement("canvas");
    small.width = 64; small.height = 48;
    const sg = small.getContext("2d", { willReadFrequently: true })!;
    navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false }).then((s) => {
      stream.current = s;
      if (video.current) { video.current.srcObject = s; video.current.play().catch(() => {}); }
      timer = setInterval(() => {
        if (!video.current || video.current.readyState < 2) return;
        sg.drawImage(video.current, 0, 0, 64, 48);
        const px = sg.getImageData(0, 0, 64, 48).data;
        if (prev) {
          let diff = 0;
          for (let i = 0; i < px.length; i += 4) diff += Math.abs(px[i] - prev[i]) > 28 ? 1 : 0;
          const m = diff / (px.length / 4);
          setMotion(m);
          onPresence(m);
        }
        prev = new Uint8ClampedArray(px);
      }, 500);
    }).catch((e) => { setErr(e?.name === "NotAllowedError" ? "Camera permission was refused in the browser." : "No camera found."); setOn(false); });
    return () => { clearInterval(timer); stream.current?.getTracks().forEach((t) => t.stop()); stream.current = null; };
  }, [on, onPresence]);
  return (
    <section className={`${styles.panel} ${styles.cam}`}>
      <header><h2>Camera</h2><button onClick={() => { setErr(null); setOn(!on); }}>{on ? "Turn off" : "Turn on"}</button></header>
      <div className={styles.camview}>
        {on ? <video ref={video} muted playsInline /> : <p className={styles.dim}>Off. Turn it on to see the room and let {NAME} notice when you sit down. Frames stay in this browser.</p>}
        {on && <div className={styles.scan} />}
      </div>
      {err && <p className={styles.warn}>{err}</p>}
      {on && <div className={styles.meter}><span style={{ width: `${Math.min(100, motion * 400)}%` }} /><em>motion {Math.round(motion * 100)}%</em></div>}
    </section>
  );
}

/* ------------------------------------------------------------------ page */
export default function Deck() {
  const router = useRouter();
  const [clock, setClock] = useState("");
  const [sys, setSys] = useState<any>(null);
  const [pf, setPf] = useState<any>(null);
  const [scr, setScr] = useState<any>(null);
  const [news, setNews] = useState<any>(null);
  const [apiErr, setApiErr] = useState<string | null>(null);
  const [log, setLog] = useState<Line[]>([]);
  const [mode, setMode] = useState<"idle" | "listening" | "speaking" | "alert">("idle");
  const [level, setLevel] = useState(0);
  const [typed, setTyped] = useState("");
  const [voiceOk, setVoiceOk] = useState(true);
  const [armKill, setArmKill] = useState(false);
  const [greet, setGreet] = useState(true);
  const lastMotion = useRef(Date.now());
  const greeted = useRef(0);
  const audio = useRef<{ ctx: AudioContext; stop: () => void } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([api("/system"), api("/screening/status")]);
      setSys(s); setScr(st); setApiErr(null);
      if (st?.started) api("/screening/portfolio").then(setPf).catch(() => setPf(null));
    } catch (e: any) { setApiErr(e.message); }
  }, []);
  const loadNews = useCallback(async (refreshFeeds = false) => {
    try { setNews(await api(`/news?limit=24${refreshFeeds ? "&refresh=true" : ""}`)); } catch (e: any) { setNews({ items: [], errors: { news: e.message } }); }
  }, []);
  useEffect(() => {
    refresh(); loadNews();
    const a = setInterval(refresh, 15000), b = setInterval(() => loadNews(), 600000), c = setInterval(() => setClock(istNow()), 1000);
    setVoiceOk(typeof window !== "undefined" && !!((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition));
    return () => { clearInterval(a); clearInterval(b); clearInterval(c); };
  }, [refresh, loadNews]);
  useEffect(() => { if (sys?.kill_switch?.engaged) setMode("alert"); else if (mode === "alert") setMode("idle"); }, [sys]);   // eslint-disable-line

  const say = useCallback((text: string) => {
    setLog((l) => [...l.slice(-30), { who: "vega", text, at: istNow() }]);
    const synth = window.speechSynthesis;
    if (!synth) return;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text.replace(/₹/g, "rupees ").slice(0, 600));
    const v = synth.getVoices();
    u.voice = v.find((x) => /en-IN/.test(x.lang)) || v.find((x) => /en-GB/.test(x.lang)) || v.find((x) => /^en/.test(x.lang)) || null;
    u.rate = 1.02;
    u.onstart = () => setMode((m) => (m === "alert" ? m : "speaking"));
    u.onend = () => { setMode((m) => (m === "alert" ? m : "idle")); setLevel(0); };
    u.onboundary = () => setLevel(0.4 + Math.random() * 0.5);
    synth.speak(u);
  }, []);

  const statusLine = useCallback(() => {
    if (!sys) return "I can't reach the lab's back end. Check that the API window is running.";
    const parts = [`Live trading is ${sys.live_trading_enabled ? "ON, which it should not be" : "off"}.`,
      sys.kill_switch?.engaged ? "The kill switch is engaged." : "The kill switch is clear."];
    if (scr?.started) parts.push(`Screening ${scr.registered} strategies on ${scr.symbols} symbols, last minute ${String(scr.watermark || "none").slice(11, 16)}.`);
    else parts.push("No screening run yet.");
    if (pf?.equity_inr != null) parts.push(`Paper equity ${inr(pf.equity_inr)}, ${pf.open_positions} open positions${pf.halted_today ? ", new entries halted for today by the loss limit" : ""}.`);
    return parts.join(" ");
  }, [sys, scr, pf]);

  const handle = useCallback(async (raw: string) => {
    const q = raw.trim();
    if (!q) return;
    setLog((l) => [...l.slice(-30), { who: "you", text: q, at: istNow() }]);
    const s = q.toLowerCase();
    try {
      if (/\b(buy|sell|place (an )?order|go long|go short|square off)\b/.test(s) && !/\bnews\b/.test(s))
        return say("Real orders are disabled in this lab, and I can't place them. Strategies trade on paper only.");
      if (/(reset|clear|release).*(kill|switch)|resume trading/.test(s))
        return say("Only a named person can reset the kill switch, on the Risk Control Center page. I won't do it by voice.");
      if (/\b(kill switch|stop (all )?trading|emergency stop|halt everything)\b/.test(s)) {
        await post("/risk/kill", { operator: "voice command (Command Deck)", reason: `spoken: "${q}"` });
        await refresh();
        return say("Kill switch engaged. All paper sessions are halted and no new simulated orders will be placed.");
      }
      if (/\b(news|headlines?|world)\b/.test(s)) {
        const n = news?.items?.length ? news : await api("/news?limit=24");
        setNews(n);
        const top = (n.items || []).slice(0, 4).map((x: any) => `${x.title}, from ${x.source}.`);
        return say(top.length ? `Top headlines. ${top.join(" ")}` : "I couldn't load any headlines. The news feeds may be unreachable.");
      }
      if (/\b(portfolio|equity|positions?|cash|p ?and ?l|pnl)\b/.test(s)) {
        if (!pf) return say("There's no screening portfolio yet. Start a replay, or set up Angel One for live data.");
        const top = (pf.top_symbols || []).slice(0, 3).map((x: any) => `${x.symbol} ${(x.pct_equity * 100).toFixed(1)} percent`).join(", ");
        return say(`Paper equity ${inr(pf.equity_inr)}. Free cash ${inr(pf.cash_inr - pf.reserved_inr)}. ${pf.open_positions} open positions${top ? `, largest ${top}` : ""}.`);
      }
      if (/\breplay\b/.test(s)) {
        await post("/screening/replay", { sessions: 5 });
        return say("Started a five-session replay of the screening loop. Results appear on the Strategy Monitor in about a minute.");
      }
      if (/\bresearch\b/.test(s)) {
        await post("/research/run", { max_iterations: 1, max_backtests: 120 });
        return say("Research run started. The team will propose, test and critique a hypothesis; check the Command Center for the result.");
      }
      if (/\b(status|report|how are we|how is everything|system)\b/.test(s)) return say(statusLine());
      if (/\b(open|show|go to)\b/.test(s)) {
        const hit = PAGES.find(([re]) => re.test(s));
        if (hit) { say(`Opening ${hit[2]}.`); setTimeout(() => router.push(hit[1]), 600); return; }
      }
      const r = await post("/chat", { question: q });
      return say(String(r.answer || "I don't have an answer for that.").replace(/\s+/g, " ").slice(0, 700));
    } catch (e: any) {
      return say(`That didn't work: ${e.message}`);
    }
  }, [news, pf, refresh, router, say, statusLine]);

  const listen = useCallback(async () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return;
    window.speechSynthesis?.cancel();
    const rec = new SR();
    rec.lang = "en-IN"; rec.interimResults = false; rec.maxAlternatives = 1;
    try {                                                              // a live level for the reactor while listening
      const ms = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new AudioContext(), src = ctx.createMediaStreamSource(ms), an = ctx.createAnalyser();
      an.fftSize = 256; src.connect(an);
      const buf = new Uint8Array(an.frequencyBinCount);
      let raf = 0;
      const tick = () => { an.getByteFrequencyData(buf); setLevel(Math.min(1, buf.reduce((a, b) => a + b, 0) / buf.length / 90)); raf = requestAnimationFrame(tick); };
      tick();
      audio.current = { ctx, stop: () => { cancelAnimationFrame(raf); ms.getTracks().forEach((t) => t.stop()); ctx.close(); setLevel(0); } };
    } catch { /* level meter is optional */ }
    const done = () => { audio.current?.stop(); audio.current = null; setMode((m) => (m === "listening" ? "idle" : m)); };
    rec.onresult = (ev: any) => { done(); handle(ev.results[0][0].transcript); };
    rec.onerror = (ev: any) => { done(); if (ev.error !== "no-speech" && ev.error !== "aborted") say(`I couldn't hear that (${ev.error}).`); };
    rec.onend = done;
    setMode("listening");
    rec.start();
  }, [handle, say]);

  useEffect(() => {                                                   // space bar = push to talk
    const k = (e: KeyboardEvent) => { if (e.code === "Space" && (e.target as HTMLElement)?.tagName !== "INPUT" && voiceOk) { e.preventDefault(); listen(); } };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [listen, voiceOk]);

  const onPresence = useCallback((m: number) => {
    const now = Date.now();
    if (m > 0.04) {
      if (greet && now - lastMotion.current > 120000 && now - greeted.current > 600000) { greeted.current = now; say(`Welcome back. ${statusLine()}`); }
      lastMotion.current = now;
    }
  }, [greet, say, statusLine]);

  const kill = sys?.kill_switch?.engaged;
  const items = news?.items || [];
  return (
    <div className={styles.deck}>
      <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500&family=Chakra+Petch:wght@400;600&display=swap" />
      <header className={styles.top}>
        <Link href="/" className={styles.exit}>Exit deck</Link>
        <div className={styles.ident}><b>{NAME}</b><span>NSE AI Quant Lab</span></div>
        <div className={styles.chips}>
          <span className={sys?.live_trading_enabled ? styles.bad : styles.good}>{sys ? (sys.live_trading_enabled ? "Live flag on" : "Live trading off") : "…"}</span>
          <span className={kill ? styles.bad : styles.good}>{kill ? "Kill switch engaged" : "Kill switch clear"}</span>
          <time>{clock} IST</time>
        </div>
      </header>
      {apiErr && <p className={styles.alert}>The lab's back end isn't answering ({apiErr}). Start it with scripts\windows\start.ps1, or check experiments\logs\api.log.</p>}

      <main className={styles.grid}>
        <section className={`${styles.panel} ${styles.sys}`}>
          <header><h2>Systems</h2></header>
          <dl>
            <div><dt>Screening</dt><dd>{scr?.started ? `${scr.registered?.toLocaleString("en-IN")} strategies · ${scr.symbols} symbols` : "not started"}</dd></div>
            <div><dt>Feed</dt><dd>{scr?.feed || scr?.live?.state || "—"}</dd></div>
            <div><dt>Last minute</dt><dd>{scr?.watermark ? String(scr.watermark).replace("T", " ").slice(0, 16) : "—"}</dd></div>
            <div><dt>Sandbox</dt><dd>{sys ? (sys.sandbox?.network_namespace ? "isolated" : "off on this machine") : "—"}</dd></div>
            <div><dt>Analyst</dt><dd>{sys?.llm?.configured ? "Claude connected" : "rule-based (no API key)"}</dd></div>
          </dl>
          <header className={styles.sub}><h2>Paper portfolio</h2></header>
          {pf ? <dl>
            <div><dt>Equity</dt><dd className={styles.big}>{inr(pf.equity_inr)}</dd></div>
            <div><dt>Free cash</dt><dd>{inr(pf.cash_inr - pf.reserved_inr)}</dd></div>
            <div><dt>Open positions</dt><dd>{pf.open_positions} of {pf.rules?.max_positions ?? "—"}</dd></div>
            <div><dt>Today</dt><dd className={pf.halted_today ? styles.warnTxt : ""}>{pf.halted_today ? "loss limit hit, entries halted" : "within limits"}</dd></div>
          </dl> : <p className={styles.dim}>No portfolio yet. Say “run replay”, or set up Angel One for live data.</p>}
        </section>

        <section className={styles.core}>
          <Reactor level={level} mode={kill ? "alert" : mode} />
          <button className={styles.mic} onClick={listen} disabled={!voiceOk || mode === "listening"} aria-label="Talk to Vega">
            {mode === "listening" ? "Listening…" : voiceOk ? "Talk (or press space)" : "Voice needs Chrome or Edge"}
          </button>
          <form className={styles.ask} onSubmit={(e) => { e.preventDefault(); handle(typed); setTyped(""); }}>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={`Ask ${NAME}: status, news, portfolio, run replay, open risk…`} aria-label="Type a command" />
          </form>
          <ol className={styles.log} aria-live="polite">
            {log.slice(-6).map((l, i) => <li key={i} className={l.who === "you" ? styles.you : styles.vega}><time>{l.at}</time>{l.text}</li>)}
            {!log.length && <li className={styles.dim}>Try “give me a status report” or “what's the world news”.</li>}
          </ol>
        </section>

        <section className={`${styles.panel} ${styles.news}`}>
          <header><h2>World and markets</h2><button onClick={() => loadNews(true)}>Refresh</button></header>
          <ul>
            {items.map((n: any, i: number) => (
              <li key={i}><a href={n.link} target="_blank" rel="noreferrer">{n.title}</a><small>{n.source}{n.published ? ` · ${String(n.published).slice(11, 16)}` : ""}</small></li>))}
            {!items.length && <li className={styles.dim}>{news ? "No headlines loaded. Is this computer online?" : "Loading headlines…"}</li>}
          </ul>
          {news?.errors && Object.keys(news.errors).length > 0 && <p className={styles.dim}>Unavailable: {Object.keys(news.errors).join(", ")}</p>}
        </section>

        <section className={`${styles.panel} ${styles.actions}`}>
          <header><h2>Actions</h2><label className={styles.toggle}><input type="checkbox" checked={greet} onChange={(e) => setGreet(e.target.checked)} /> Greet me when the camera sees me</label></header>
          <div className={styles.btns}>
            <button onClick={() => handle("status report")}>Status report</button>
            <button onClick={() => handle("read the headlines")}>Read the headlines</button>
            <button onClick={() => handle("portfolio")}>Portfolio summary</button>
            <button onClick={() => handle("run replay")}>Run a replay</button>
            <button onClick={() => handle("run research")}>Run research</button>
            <button onClick={() => router.push("/screening")}>Open Strategy Monitor</button>
            {!armKill
              ? <button className={styles.killBtn} onClick={() => setArmKill(true)} disabled={kill}>{kill ? "Kill switch engaged" : "Engage kill switch"}</button>
              : <button className={styles.killBtn} onClick={() => { setArmKill(false); handle("kill switch"); }}>Confirm: halt everything</button>}
          </div>
          <p className={styles.dim}>Voice and buttons run the same actions as the dashboard. Real orders are disabled; resetting the kill switch needs a named person on the Risk page.</p>
        </section>

        <Camera onPresence={onPresence} />
      </main>
    </div>
  );
}
