"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import React, { useCallback, useEffect, useState } from "react";
import { api, post, setToken, useApi } from "@/lib/api";
import Copilot from "./Copilot";
import { NAV } from "@/lib/nav";
import { provKind } from "./ui";

/* Workspaces (lib/nav.ts): only pages that exist and work are listed. Planned workspaces without a working journey are
   tracked in docs/AUDIT_V6.md, not shown as empty pages. */


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
  return (
    <div className="shell">
      <a href="#content" className="skip">Skip to content</a>
      <aside className={`side ${open ? "open" : ""}`} aria-label="Workspaces">
        <div className="brand"><span className="mark" aria-hidden="true">Q</span><span><b>NSE AI Quant Lab</b><small>research &amp; paper trading</small></span></div>
        <nav className="nav">
          {NAV.map(([g, items]) => (
            <div key={g}><div className="navg">{g}</div>
              {items.map(([href, label]) => <Link key={href} href={href} className={path === href ? "on" : ""} aria-current={path === href ? "page" : undefined}>{label}</Link>)}</div>))}
        </nav>
      </aside>
      <header className="strip" aria-label="Platform status">
        <button className="menu" onClick={() => setOpen(!open)} aria-expanded={open} aria-label="Open navigation">Menu</button>
        <div className="chips">
        {ps.error && <span className="chip crit" data-testid="strip-conn"><i />✕ API offline</span>}
        {!s && !ps.error && <span className="chip info" data-testid="strip-conn"><i />Connecting…</span>}
        {s && <>
          <span className={`chip ${s.live_trading_enabled ? "crit" : "good"}`} data-testid="strip-mode"><i />{s.live_trading_enabled ? "▲ Live flag ON" : "◆ Paper mode · real orders off"}</span>
          <span className={`chip ${s.market.open ? "good" : "info"}`} title={`IST ${s.market.ist_time}`}><i />Market {s.market.state}</span>
          <span className="chip info" data-testid="strip-candle" title="last completed 1-minute candle processed by the bot">
            <i />{s.last_candle ? `Last candle ${String(s.last_candle).slice(11, 16)} IST ${String(s.last_candle).slice(8, 10)}/${String(s.last_candle).slice(5, 7)}` : "No candle processed"}</span>
          <span className={`chip ${k.q === "live" ? "good" : k.q === "synthetic" || k.q === "unverified" ? "warn" : "info"}`} title={ds?.label} data-testid="strip-data">
            <i />{k.q === "live" ? "✓ " : k.q === "synthetic" ? "◇ " : k.q === "unverified" ? "! " : ""}{ds?.label}</span>
          <span className={`chip ${kill ? "crit" : "good"}`} data-testid="strip-kill"><i />{kill ? "■ Kill switch engaged" : "Kill switch clear"}</span>
          {s.modules.attention.length > 0 && <Link href="/" className="chip warn"><i />! {s.modules.attention.length} module{s.modules.attention.length > 1 ? "s" : ""} need attention</Link>}
        </>}
        </div>
        <div className="acts">
        <button className="ghost theme" onClick={flip} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>{theme === "dark" ? "Light" : "Dark"}</button>
        <button onClick={() => setVega(true)} aria-haspopup="dialog" data-testid="open-vega" title="Vega: the global assistant; answers from platform-wide records">Ask Vega <span className="dim kbd">Ctrl K</span></button>
        {path !== "/deck" && <button className="estop" onClick={estop} disabled={!s || kill || viewer} data-testid="strip-estop"
          title={viewer ? "Read-only token: the operator token is needed" : "Halts all paper trading at once; no confirmation"}>{kill ? "Halted" : "Emergency stop"}</button>}
        </div>
      </header>
      <main className="main" id="content" tabIndex={-1}>
        {stopMsg && <div className="banner crit" role="alert">{stopMsg}</div>}
        {children}
      </main>
      {vega && <Copilot onClose={() => setVega(false)} page={path} />}
    </div>
  );
}
