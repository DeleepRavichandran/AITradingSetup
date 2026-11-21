"use client";
/* Command Deck: a voice-driven, full-screen view of the lab.

   Safety: commands are parsed by lib/vega.ts (unit-tested). Anything that changes state (kill switch, replay,
   research) asks for confirmation - say "confirm" or press Confirm within 10 seconds - and low-confidence
   speech never acts. The Emergency stop button in the header is separate and immediate. Nothing here can
   reset a safety mechanism or place an order.
   Privacy: the camera stays in this browser (frames are only compared locally for motion). Speech
   recognition in Chrome and Edge is done by the browser maker's online service, so audio leaves this
   computer while the microphone is listening; the page asks once before using it. */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { CONFIRM_WINDOW_MS, describeOperational, OPERATIONAL, parse, type Intent } from "@/lib/vega";
import styles from "./deck.module.css";

type Line = { who: "you" | "vega"; text: string; at: string };
type Perm = "granted" | "denied" | "prompt" | "unsupported" | "unknown";
const NAME = "Vega";
const VOICE_CONSENT_KEY = "nseql_voice_consent";
const istNow = () => new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const inr = (x: number | null | undefined) => (x == null ? "—" : `₹${Math.round(x).toLocaleString("en-IN")}`);
const store = { get: (k: string) => { try { return window.localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { window.localStorage.setItem(k, v); } catch { /* private mode */ } } };

function usePermission(name: "camera" | "microphone"): Perm {
  const [p, setP] = useState<Perm>("unknown");
  useEffect(() => {
    let status: PermissionStatus | null = null;
    const nav: any = typeof navigator !== "undefined" ? navigator : null;
    if (!nav?.mediaDevices?.getUserMedia) { setP("unsupported"); return; }
    if (!nav.permissions?.query) { setP("prompt"); return; }
    nav.permissions.query({ name }).then((s: PermissionStatus) => {
      status = s; setP(s.state as Perm);
      s.onchange = () => setP(s.state as Perm);
    }).catch(() => setP("prompt"));
    return () => { if (status) status.onchange = null; };
  }, [name]);
  return p;
}

/* ------------------------------------------------------------------ the reactor */
function Reactor({ level, mode }: { level: number; mode: "idle" | "listening" | "speaking" | "alert" | "confirm" }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const lv = useRef(level);
  lv.current = level;
  useEffect(() => {
    const c = ref.current!;
    const g = c.getContext("2d");
    if (!g) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0, t = 0;
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.42;
      const hue = mode === "alert" ? "255,77,94" : mode === "speaking" || mode === "confirm" ? "255,181,71" : "95,227,255";
      const pulse = 1 + lv.current * 0.18;
      t += reduce ? 0 : 0.006 + lv.current * 0.04;
      const ring = (r: number, segs: number, gap: number, width: number, alpha: number, rot: number) => {
        g.lineWidth = width; g.strokeStyle = `rgba(${hue},${alpha})`;
        for (let i = 0; i < segs; i++) { const a0 = rot + (i / segs) * Math.PI * 2; g.beginPath(); g.arc(cx, cy, r, a0, a0 + (Math.PI * 2) / segs - gap); g.stroke(); }
      };
      ring(R * pulse, 3, 0.5, 1.5, 0.35, t * 0.6);
      ring(R * 0.86, 48, 0.07, 6, 0.55, -t);
      ring(R * 0.74, 6, 0.35, 2, 0.8, t * 1.7);
      ring(R * 0.62, 24, 0.12, 10, 0.25 + lv.current * 0.5, -t * 0.5);
      const core = g.createRadialGradient(cx, cy, 0, cx, cy, R * 0.48 * pulse);
      core.addColorStop(0, `rgba(${hue},0.95)`); core.addColorStop(0.35, `rgba(${hue},0.35)`); core.addColorStop(1, `rgba(${hue},0)`);
      g.fillStyle = core; g.beginPath(); g.arc(cx, cy, R * 0.5 * pulse, 0, Math.PI * 2); g.fill();
      for (let i = 0; i < 60; i++) {
        const a = (i / 60) * Math.PI * 2 + t * 0.15, r0 = R * 1.04, r1 = R * (i % 5 ? 1.08 : 1.13);
        g.strokeStyle = `rgba(${hue},${i % 5 ? 0.25 : 0.6})`; g.lineWidth = 1;
        g.beginPath(); g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0); g.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1); g.stroke();
      }
      raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [mode]);
  return <canvas ref={ref} className={styles.reactor} aria-hidden="true" data-testid="reactor" />;
}

/* ------------------------------------------------------------------ camera */
function Camera({ onPresence }: { onPresence: (motion: number) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const perm = usePermission("camera");
  const [on, setOn] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [motion, setMotion] = useState(0);
  useEffect(() => {
    if (!on) return;
    let timer: ReturnType<typeof setInterval> | null = null, stream: MediaStream | null = null, cancelled = false;
    let prev: Uint8ClampedArray | null = null;
    const small = document.createElement("canvas");
    small.width = 64; small.height = 48;
    const sg = small.getContext("2d", { willReadFrequently: true });
    navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false }).then((s) => {
      if (cancelled) { s.getTracks().forEach((t) => t.stop()); return; }
      stream = s;
      if (video.current) { video.current.srcObject = s; video.current.play().catch(() => {}); }
      timer = setInterval(() => {
        if (!sg || !video.current || video.current.readyState < 2) return;
        sg.drawImage(video.current, 0, 0, 64, 48);
        const px = sg.getImageData(0, 0, 64, 48).data;
        if (prev) {
          let diff = 0;
          for (let i = 0; i < px.length; i += 4) diff += Math.abs(px[i] - prev[i]) > 28 ? 1 : 0;
          const m = diff / (px.length / 4);
          setMotion(m); onPresence(m);
        }
        prev = new Uint8ClampedArray(px);
      }, 500);
    }).catch((e) => {
      if (cancelled) return;
      setErr(e?.name === "NotAllowedError" ? "Camera access was refused. Allow it from the camera icon in the address bar, then turn the camera on again."
        : e?.name === "NotFoundError" ? "No camera was found on this computer." : `The camera could not start (${e?.name || "unknown error"}).`);
      setOn(false);
    });
    return () => {                                                     // unmount or turn-off: release the camera
      cancelled = true;
      if (timer) clearInterval(timer);
      stream?.getTracks().forEach((t) => t.stop());
      if (video.current) video.current.srcObject = null;
    };
  }, [on, onPresence]);
  return (
    <section className={`${styles.panel} ${styles.cam}`} aria-labelledby="cam-h">
      <header><h2 id="cam-h">Camera</h2><button onClick={() => { setErr(null); setOn(!on); }} disabled={perm === "unsupported"} aria-pressed={on}>{on ? "Turn off" : "Turn on"}</button></header>
      <div className={styles.camview}>
        {on ? <video ref={video} muted playsInline aria-label="Camera preview" data-testid="camera-video" /> : <p className={styles.dim}>Off. When on, {NAME} notices when you sit down. Video stays in this browser: only a motion level is calculated, nothing is uploaded or recorded.</p>}
        {on && <div className={styles.scan} />}
      </div>
      <p className={styles.dim} data-testid="camera-permission">Camera permission: {perm === "unknown" ? "checking" : perm === "prompt" ? "not asked yet" : perm}{perm === "denied" ? ". Re-enable it from the camera icon in the address bar." : ""}</p>
      {err && <p className={styles.warn} role="alert">{err}</p>}
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
  const [mode, setMode] = useState<"idle" | "listening" | "speaking" | "alert" | "confirm">("idle");
  const [level, setLevel] = useState(0);
  const [typed, setTyped] = useState("");
  const [voiceOk, setVoiceOk] = useState(false);
  const [consent, setConsent] = useState<"yes" | "no" | "ask">("no");
  const [pending, setPending] = useState<{ intent: Intent; until: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [estop, setEstop] = useState<string | null>(null);
  const [greet, setGreet] = useState(true);
  const micPerm = usePermission("microphone");
  const lastMotion = useRef(Date.now());
  const greeted = useRef(0);
  const lastCmd = useRef<{ text: string; at: number }>({ text: "", at: 0 });
  const audio = useRef<{ stop: () => void } | null>(null);
  const rec = useRef<any>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([api("/system"), api("/screening/status")]);
      if (!mounted.current) return;
      setSys(s); setScr(st); setApiErr(null);
      if (st?.started) api("/screening/portfolio").then((p) => mounted.current && setPf(p)).catch(() => mounted.current && setPf(null));
    } catch (e: any) { if (mounted.current) setApiErr(e.message); }
  }, []);
  const loadNews = useCallback(async (refreshFeeds = false) => {
    try { const n = await api(`/news?limit=24${refreshFeeds ? "&refresh=true" : ""}`); if (mounted.current) setNews(n); }
    catch (e: any) { if (mounted.current) setNews({ items: [], errors: { news: e.message } }); }
  }, []);

  useEffect(() => {
    mounted.current = true;
    refresh(); loadNews(); setClock(istNow());
    const a = setInterval(refresh, 15000), b = setInterval(() => loadNews(), 600000), c = setInterval(() => setClock(istNow()), 1000);
    setVoiceOk(!!((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition));
    setConsent(store.get(VOICE_CONSENT_KEY) === "yes" ? "yes" : "no");
    return () => {                                                     // unmount: stop every timer, the mic and speech
      mounted.current = false;
      clearInterval(a); clearInterval(b); clearInterval(c);
      try { rec.current?.abort(); } catch { /* already stopped */ }
      audio.current?.stop(); audio.current = null;
      window.speechSynthesis?.cancel();
    };
  }, [refresh, loadNews]);

  const kill = !!sys?.kill_switch?.engaged;
  useEffect(() => {                                                    // a pending confirmation expires
    if (!pending) return;
    const id = setTimeout(() => { setPending(null); setMode((m) => (m === "confirm" ? "idle" : m)); }, Math.max(0, pending.until - Date.now()));
    return () => clearTimeout(id);
  }, [pending]);

  const say = useCallback((text: string) => {
    if (!mounted.current) return;
    setLog((l) => [...l.slice(-30), { who: "vega", text, at: istNow() }]);
    const synth = window.speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance === "undefined") return;
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text.replace(/₹/g, "rupees ").slice(0, 600));
    const v = synth.getVoices();
    u.voice = v.find((x) => /en-IN/.test(x.lang)) || v.find((x) => /en-GB/.test(x.lang)) || v.find((x) => /^en/.test(x.lang)) || null;
    u.onstart = () => setMode((m) => (m === "alert" || m === "confirm" ? m : "speaking"));
    u.onend = () => { if (!mounted.current) return; setMode((m) => (m === "speaking" ? "idle" : m)); setLevel(0); };
    u.onboundary = () => setLevel(0.4 + Math.random() * 0.5);
    synth.speak(u);
  }, []);

  const statusLine = useCallback(() => {
    if (!sys) return "I can't reach the lab's back end. Check that the API window is running.";
    const ds = scr?.data_source;
    const parts = [`Live trading is ${sys.live_trading_enabled ? "ON, which it should not be" : "off"}.`, kill ? "The kill switch is engaged." : "The kill switch is clear."];
    if (scr?.started) parts.push(`Screening ${scr.registered} strategies on ${scr.symbols} symbols. Data: ${ds?.label || scr.feed}.`);
    else parts.push("No screening run yet.");
    if (pf?.equity_inr != null) parts.push(`Paper equity ${inr(pf.equity_inr)}, ${pf.open_positions} open positions${pf.halted_today ? ", new entries halted for today by the loss limit" : ""}.`);
    return parts.join(" ");
  }, [sys, scr, pf, kill]);

  const emergencyStop = useCallback(async (operator: string, reason: string) => {
    setEstop("Stopping…");
    try {
      await post("/risk/kill", { operator, reason });
      await refresh();
      setEstop(null);
      say("Kill switch engaged. All paper sessions are halted and no new simulated orders will be placed. Only a named person can reset it, on the Risk page.");
    } catch (e: any) { setEstop(`Emergency stop failed: ${e.message}. Use the Risk Control Center page.`); }
  }, [refresh, say]);

  const execute = useCallback(async (intent: Intent) => {
    if (intent.kind === "kill") return emergencyStop("voice command (Command Deck, confirmed)", "spoken kill-switch command, confirmed");
    if (intent.kind === "replay") { await post("/screening/replay", { sessions: 5 }); return say("Started a five-session replay. Results appear on the Strategy Monitor in about a minute."); }
    if (intent.kind === "research") { await post("/research/run", { max_iterations: 1, max_backtests: 120 }); return say("Research run started. Check the Command Center for the result."); }
  }, [emergencyStop, say]);

  const handle = useCallback(async (raw: string, confidence?: number) => {
    const text = raw.trim();
    if (!text) return;
    const now = Date.now();
    if (text.toLowerCase() === lastCmd.current.text && now - lastCmd.current.at < 2000) return;   // the same words twice in 2 s: once
    lastCmd.current = { text: text.toLowerCase(), at: now };
    setLog((l) => [...l.slice(-30), { who: "you", text, at: istNow() }]);
    const intent = parse(text, confidence);
    if (intent.kind === "confirm") {
      if (!pending || pending.until < Date.now()) return say("There is nothing waiting for confirmation.");
      const p = pending; setPending(null); setMode((m) => (m === "confirm" ? "idle" : m));
      if (busy) return say("Still working on the last command.");
      setBusy(true);
      try { await execute(p.intent); } catch (e: any) { say(`That didn't work: ${e.message}`); } finally { if (mounted.current) setBusy(false); }
      return;
    }
    if (intent.kind === "cancel") { if (pending) { setPending(null); setMode((m) => (m === "confirm" ? "idle" : m)); return say("Cancelled. Nothing was changed."); } return; }
    if (pending) { setPending(null); setMode((m) => (m === "confirm" ? "idle" : m)); }      // any other words cancel a pending action
    if (OPERATIONAL.has(intent.kind)) {
      setPending({ intent, until: Date.now() + CONFIRM_WINDOW_MS }); setMode("confirm");
      return say(`Do you want me to ${describeOperational(intent.kind)}? Say "confirm" or press Confirm within ten seconds. Anything else cancels.`);
    }
    try {
      switch (intent.kind) {
        case "refuse_order": return say("Real orders are disabled in this lab, and I can't place them. Strategies trade on paper only.");
        case "refuse_reset": return say("I can't reset or disable a safety mechanism. A named person must do that on the Risk Control Center page.");
        case "clarify": return say(intent.reason);
        case "status": return say(statusLine());
        case "news": {
          const n = news?.items?.length ? news : await api("/news?limit=24");
          if (mounted.current) setNews(n);
          const top = (n.items || []).slice(0, 4).map((x: any) => `${x.title}, from ${x.source}.`);
          return say(top.length ? `Top headlines. ${top.join(" ")}` : "I couldn't load any headlines. The news feeds may be unreachable from this computer.");
        }
        case "portfolio": {
          if (!pf) return say("There's no screening portfolio yet. Start a replay, or set up Angel One for live data.");
          const top = (pf.top_symbols || []).slice(0, 3).map((x: any) => `${x.symbol} ${(x.pct_equity * 100).toFixed(1)} percent`).join(", ");
          return say(`Paper equity ${inr(pf.equity_inr)}. Free cash ${inr(pf.cash_inr - pf.reserved_inr)}. ${pf.open_positions} open positions${top ? `, largest ${top}` : ""}.`);
        }
        case "open": { say(`Opening ${intent.name}.`); setTimeout(() => mounted.current && router.push(intent.path), 600); return; }
        case "ask": { const r = await post("/chat", { question: intent.text }); return say(String(r.answer || "I don't have an answer for that.").replace(/\s+/g, " ").slice(0, 700)); }
        default: return;
      }
    } catch (e: any) { return say(`That didn't work: ${e.message}`); }
  }, [busy, execute, news, pending, pf, router, say, statusLine]);

  const stopListening = useCallback(() => {
    audio.current?.stop(); audio.current = null;
    if (mounted.current) setMode((m) => (m === "listening" ? (pending ? "confirm" : "idle") : m));
  }, [pending]);

  const listen = useCallback(async () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR || mode === "listening") return;
    if (consent !== "yes") { setConsent("ask"); return; }
    window.speechSynthesis?.cancel();
    const r = new SR();
    rec.current = r;
    r.lang = "en-IN"; r.interimResults = false; r.maxAlternatives = 1; r.continuous = false;
    try {
      const ms = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
      const ctx = new AC(), src = ctx.createMediaStreamSource(ms), an = ctx.createAnalyser();
      an.fftSize = 256; src.connect(an);
      const buf = new Uint8Array(an.frequencyBinCount);
      let raf = 0;
      const tick = () => { an.getByteFrequencyData(buf); setLevel(Math.min(1, buf.reduce((a: number, b: number) => a + b, 0) / buf.length / 90)); raf = requestAnimationFrame(tick); };
      tick();
      audio.current = { stop: () => { cancelAnimationFrame(raf); ms.getTracks().forEach((t) => t.stop()); ctx.close().catch(() => {}); setLevel(0); } };
    } catch (e: any) {
      if (e?.name === "NotAllowedError") { say("Microphone access was refused. Allow it from the microphone icon in the address bar, or type your command."); return; }
      if (e?.name === "NotFoundError") { say("No microphone was found. You can type commands instead."); return; }
    }
    r.onresult = (ev: any) => { stopListening(); const res = ev.results[0][0]; handle(res.transcript, typeof res.confidence === "number" && res.confidence > 0 ? res.confidence : undefined); };
    r.onerror = (ev: any) => {
      stopListening();
      const msg: Record<string, string> = {
        "not-allowed": "Microphone access was refused. Allow it from the microphone icon in the address bar.",
        "service-not-allowed": "The browser's speech service is turned off or blocked. Use Chrome or Edge, or type your command.",
        network: "The browser's speech service is unreachable. Speech recognition needs an internet connection.",
        "audio-capture": "No microphone was found.", "language-not-supported": "This browser can't recognise Indian English.",
      };
      if (ev.error !== "no-speech" && ev.error !== "aborted") say(msg[ev.error] || `Speech recognition failed (${ev.error}).`);
    };
    r.onend = stopListening;
    setMode("listening");
    try { r.start(); } catch { stopListening(); }
  }, [consent, handle, mode, say, stopListening]);

  useEffect(() => {                                                    // space bar = push to talk (not while typing)
    const k = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (e.code === "Space" && tag !== "INPUT" && tag !== "BUTTON" && tag !== "TEXTAREA" && voiceOk) { e.preventDefault(); listen(); }
    };
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

  const items = news?.items || [];
  const ds = scr?.data_source;
  return (
    <div className={styles.deck}>
      <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500&family=Chakra+Petch:wght@400;600&display=swap" />
      <header className={styles.top}>
        <Link href="/" className={styles.exit}>Exit deck</Link>
        <div className={styles.ident}><b>{NAME}</b><span>NSE AI Quant Lab</span></div>
        <div className={styles.chips}>
          <span className={sys?.live_trading_enabled ? styles.bad : styles.good}>{sys ? (sys.live_trading_enabled ? "Live flag on" : "Live trading off") : "…"}</span>
          <span className={kill ? styles.bad : styles.good} data-testid="kill-state">{kill ? "Kill switch engaged" : "Kill switch clear"}</span>
          <time>{clock} IST</time>
          <button className={styles.estop} onClick={() => emergencyStop("operator (Command Deck emergency stop button)", "manual emergency stop button")}
            disabled={kill || estop === "Stopping…"} data-testid="emergency-stop">{kill ? "Halted" : "Emergency stop"}</button>
        </div>
      </header>
      {estop && estop !== "Stopping…" && <p className={styles.alert} role="alert">{estop}</p>}
      {apiErr && <p className={styles.alert} role="alert">The lab's back end isn't answering ({apiErr}). Start it with scripts\windows\start.ps1, or check experiments\logs\api.log.</p>}

      <main className={styles.grid}>
        <section className={`${styles.panel} ${styles.sys}`} aria-labelledby="sys-h">
          <header><h2 id="sys-h">Systems</h2></header>
          <dl>
            <div><dt>Market data</dt><dd data-testid="data-source" className={ds?.stale ? styles.warnTxt : ""}>{ds?.label || "—"}</dd></div>
            {ds?.delay_seconds != null && <div><dt>Delay</dt><dd>≈ {ds.delay_seconds >= 120 ? `${Math.round(ds.delay_seconds / 60)} min` : `${ds.delay_seconds} s`}</dd></div>}
            {ds?.freshness_seconds != null && <div><dt>Last bar</dt><dd>{Math.round(ds.freshness_seconds)} s ago</dd></div>}
            <div><dt>Screening</dt><dd>{scr?.started ? `${scr.registered?.toLocaleString("en-IN")} strategies · ${scr.symbols} symbols` : "not started"}</dd></div>
            <div><dt>Last minute</dt><dd>{scr?.watermark ? String(scr.watermark).replace("T", " ").slice(0, 16) : "—"}</dd></div>
            <div><dt>Sandbox</dt><dd>{sys ? (sys.sandbox?.network_namespace ? "isolated" : "unavailable (research off)") : "—"}</dd></div>
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

        <section className={styles.core} aria-label="Voice assistant">
          <Reactor level={level} mode={kill ? "alert" : mode} />
          {pending && (
            <div className={styles.confirm} role="alertdialog" aria-labelledby="confirm-t" data-testid="confirm-box">
              <p id="confirm-t">Confirm: {describeOperational(pending.intent.kind)}?</p>
              <button onClick={() => handle("confirm")} data-testid="confirm-yes">Confirm</button>
              <button onClick={() => handle("cancel")}>Cancel</button>
            </div>
          )}
          {consent === "ask" && (
            <div className={styles.confirm} role="dialog" aria-labelledby="consent-t">
              <p id="consent-t">Voice uses your browser's speech service (Google for Chrome, Microsoft for Edge): your audio is sent to them while {NAME} is listening. The microphone is only on after you press Talk.</p>
              <button onClick={() => { store.set(VOICE_CONSENT_KEY, "yes"); setConsent("yes"); }}>Allow voice</button>
              <button onClick={() => setConsent("no")}>Not now</button>
            </div>
          )}
          <button className={styles.mic} onClick={listen} disabled={!voiceOk || mode === "listening" || micPerm === "denied"} data-testid="talk">
            {mode === "listening" ? "Listening…" : !voiceOk ? "Voice needs Chrome or Edge" : micPerm === "denied" ? "Microphone blocked" : "Talk (or press space)"}
          </button>
          <p className={styles.dim} data-testid="mic-permission">Microphone: {micPerm === "unknown" ? "checking" : micPerm === "prompt" ? "not asked yet" : micPerm}
            {micPerm === "denied" ? ". Re-enable it from the microphone icon in the address bar; typing still works." : ""}</p>
          <form className={styles.ask} onSubmit={(e) => { e.preventDefault(); const t = typed; setTyped(""); handle(t); }}>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={`Ask ${NAME}: status, news, portfolio, run replay, open risk…`} aria-label="Type a command" />
          </form>
          <ol className={styles.log} aria-live="polite" data-testid="log">
            {log.slice(-6).map((l, i) => <li key={i} className={l.who === "you" ? styles.you : styles.vega}><time>{l.at}</time>{l.text}</li>)}
            {!log.length && <li className={styles.dim}>Try “give me a status report” or “what's the world news”.</li>}
          </ol>
        </section>

        <section className={`${styles.panel} ${styles.news}`} aria-labelledby="news-h">
          <header><h2 id="news-h">World and markets</h2><button onClick={() => loadNews(true)}>Refresh</button></header>
          <ul>
            {items.map((n: any, i: number) => (
              <li key={i}><a href={n.link} target="_blank" rel="noreferrer">{n.title}</a><small>{n.source}{n.published ? ` · ${String(n.published).slice(11, 16)}` : ""}</small></li>))}
            {!items.length && <li className={styles.dim}>{news ? "No headlines loaded. Is this computer online?" : "Loading headlines…"}</li>}
          </ul>
          {news?.errors && Object.keys(news.errors).length > 0 && <p className={styles.dim}>Unavailable: {Object.keys(news.errors).join(", ")}</p>}
          <p className={styles.dim}>Headlines are for reading only; no strategy uses them.</p>
        </section>

        <section className={`${styles.panel} ${styles.actions}`} aria-labelledby="act-h">
          <header><h2 id="act-h">Actions</h2><label className={styles.toggle}><input type="checkbox" checked={greet} onChange={(e) => setGreet(e.target.checked)} /> Greet me when the camera sees me</label></header>
          <div className={styles.btns}>
            <button onClick={() => handle("status report")}>Status report</button>
            <button onClick={() => handle("read the headlines")}>Read the headlines</button>
            <button onClick={() => handle("portfolio")}>Portfolio summary</button>
            <button onClick={() => handle("run replay")}>Run a replay</button>
            <button onClick={() => handle("run research")}>Run research</button>
            <button onClick={() => router.push("/screening")}>Open Strategy Monitor</button>
          </div>
          <p className={styles.dim}>Replay and research ask for confirmation, by voice or by button. The Emergency stop at the top acts at once. Real orders are disabled, and safety resets need a named person on the Risk page.</p>
        </section>

        <Camera onPresence={onPresence} />
      </main>
    </div>
  );
}
