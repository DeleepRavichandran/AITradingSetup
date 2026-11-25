"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import React, { useCallback, useEffect, useState } from "react";
import { api, post, setToken, useApi } from "@/lib/api";
import Copilot from "./Copilot";
import { AREAS, areaOf } from "@/lib/nav";
import { provKind } from "./ui";

/* VEGA shell: a compact rail of six areas (lib/nav.ts), the current area's pages as tabs, and a status bar that always
   shows paper mode, market, last candle, data source and the kill switch, with the emergency stop on every page. */
const ICON: Record<string, React.ReactNode> = {
  overview: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  radar: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><path d="M12 12L19 5" /><circle cx="15" cy="9" r="1.4" fill="currentColor" /></>,
  portfolio: <><rect x="3" y="7" width="18" height="14" rx="2" /><path d="M8 7V4h8v3M3 13h18M10 13v3h4v-3" /></>,
  research: <><path d="M9 3h6M10 3v7L4 19a1 1 0 001 2h14a1 1 0 001-2l-6-9V3M7 15h10" /></>,
  risk: <><path d="M12 3l8 3v6c0 5-4 8-8 10-4-2-8-5-8-10V6zM8 12l3 3 5-6" /></>,
  system: <><path d="M3 12h4l3-7 4 14 3-7h4" /></>,
};


function Login({ onDone }: { onDone: () => void }) {
  const [t, setT] = useState("");
  const [err, setErr] = useState("");
  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    setToken(t.trim());
    try { await api("/system"); onDone(); } catch { setErr("That token was not accepted."); }
  };
  return (
    <form className="card login" onSubmit={go}>
      <h1>NSE AI Quant Lab</h1>
      <p className="sub" style={{ marginTop: 8 }}>Enter the access token configured on the server (NSEQL_API_TOKEN). It is stored only in this browser.</p>
      <label>Access token<input type="password" value={t} onChange={(e) => setT(e.target.value)} autoFocus /></label>
      {err && <p className="down" style={{ marginTop: 8 }}>{err}</p>}
      <div style={{ marginTop: 12 }}><button className="primary" disabled={!t.trim()}>Sign in</button></div>
    </form>
  );
}

const themeStore = {
  get: () => { try { return window.localStorage.getItem("nseql_theme"); } catch { return null; } },
  set: (v: string) => { try { window.localStorage.setItem("nseql_theme", v); } catch { /* private mode */ } },
};

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [open, setOpen] = useState(false);
  const [locked, setLocked] = useState(false);
  const [vega, setVega] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [stopMsg, setStopMsg] = useState<string | null>(null);
  const ps = useApi<any>(locked ? null : "/platform/status", 15000);
  useEffect(() => {
    const h = () => setLocked(true);
    window.addEventListener("nseql-unauthorized", h);
    const t = themeStore.get() === "light" ? "light" : "dark";
    setTheme(t);
    document.documentElement.dataset.theme = t;
    const k = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setVega((x) => !x); } };
    window.addEventListener("keydown", k);
    return () => { window.removeEventListener("nseql-unauthorized", h); window.removeEventListener("keydown", k); };
  }, []);
  useEffect(() => setOpen(false), [path]);
  const flip = () => { const t = theme === "dark" ? "light" : "dark"; setTheme(t); themeStore.set(t); document.documentElement.dataset.theme = t; };
  const s = ps.data;
  const kill = !!s?.kill_switch?.engaged;
  const viewer = s?.role === "viewer";
  const estop = useCallback(async () => {
    setStopMsg(null);
    try { await post("/risk/kill", { operator: "operator (status bar emergency stop)", reason: "manual emergency stop button" }); await ps.reload(); }
    catch (e: any) { setStopMsg(`Emergency stop failed: ${e.message}. Use the Risk Control page.`); }
  }, [ps]);
  if (locked) return <Login onDone={() => { setLocked(false); window.location.reload(); }} />;
  const ds = s?.data_source, k = provKind(ds);
  const area = areaOf(path);
  return (
    <div className="shell">
      <a href="#content" className="skip">Skip to content</a>
      <nav className={`rail ${open ? "open" : ""}`} aria-label="Areas">
        <Link href="/" className="mark" aria-label="Mission Control" title="NSE AI Quant Lab">
          <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M4 7l12 21L28 7M9 7l7 13 7-13M4 7h24" /><circle cx="16" cy="7" r="2" fill="currentColor" /></svg>
        </Link>
        {AREAS.map((a) => (
          <Link key={a.key} href={a.pages[0][0]} className={`area ${a.key === area.key ? "on" : ""}`} aria-current={a.key === area.key ? "true" : undefined}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">{ICON[a.key]}</svg>{a.label}</Link>))}
        <div className="foot"><span className="chip" title="code version">{s?.version?.commit ? String(s.version.commit).slice(0, 7) : "paper"}</span></div>
      </nav>
      <header className="strip" aria-label="Platform status">
        <span className="word">VEGA <span>/ NSE QUANT LAB</span></span>
        <div className="chips">
        {ps.error && <span className="chip crit" data-testid="strip-conn"><i />✕ API offline</span>}
        {!s && !ps.error && <span className="chip info" data-testid="strip-conn"><i />Connecting…</span>}
        {s && <>
          <span className={`chip ${s.live_trading_enabled ? "crit" : "paper"}`} data-testid="strip-mode"><i />{s.live_trading_enabled ? "▲ Live flag ON" : "◆ Paper mode · real orders off"}</span>
          <span className={`chip ${s.market.open ? "good" : "info"}`} title={`IST ${s.market.ist_time}`}><i />Market {s.market.state}</span>
          <span className="chip info" data-testid="strip-candle" title="last completed 1-minute candle processed by the bot">
            <i />{s.last_candle ? `Last candle ${String(s.last_candle).slice(11, 16)} IST ${String(s.last_candle).slice(8, 10)}/${String(s.last_candle).slice(5, 7)}` : "No candle processed"}</span>
          <span className={`chip ${k.q === "live" ? "good" : k.q === "synthetic" || k.q === "unverified" ? "warn" : "info"}`} title={ds?.label} data-testid="strip-data">
            <i />{k.q === "live" ? "✓ " : k.q === "synthetic" ? "◇ " : k.q === "unverified" ? "! " : ""}{ds?.label}</span>
          <span className={`chip ${kill ? "crit" : "good"}`} data-testid="strip-kill"><i />{kill ? "■ Kill switch engaged" : "Kill switch clear"}</span>
          {s.modules.attention.length > 0 && <Link href="/command" className="chip warn"><i />! {s.modules.attention.length} module{s.modules.attention.length > 1 ? "s" : ""} need attention</Link>}
        </>}
        </div>
        <div className="acts">
        <button className="ghost theme" onClick={flip} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>{theme === "dark" ? "Light" : "Dark"}</button>
        <button onClick={() => setVega(true)} aria-haspopup="dialog" data-testid="open-vega" title="Vega: the global assistant; answers from platform-wide records">Ask Vega <span className="dim kbd">Ctrl K</span></button>
        {path !== "/deck" && <button className="estop" onClick={estop} disabled={!s || kill || viewer} data-testid="strip-estop"
          title={viewer ? "Read-only token: the operator token is needed" : "Halts all paper trading at once; no confirmation"}>{kill ? "Halted" : "Emergency stop"}</button>}
        </div>
      </header>
      <nav className="areatabs" aria-label={`${area.label} pages`}>
        {area.pages.map(([href, label]) => <Link key={href} href={href} className={path === href ? "on" : ""} aria-current={path === href ? "page" : undefined}>{label}</Link>)}
      </nav>
      <main className="main" id="content" tabIndex={-1}>
        {stopMsg && <div className="banner crit" role="alert">{stopMsg}</div>}
        {children}
      </main>
      {vega && <Copilot onClose={() => setVega(false)} page={path} />}
    </div>
  );
}
