"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import React, { useEffect, useState } from "react";
import { api, setToken, useApi } from "@/lib/api";
import { Badge } from "./ui";

const NAV: [string, [string, string][]][] = [
  ["Overview", [["/", "Command Center"], ["/market", "Market Explorer"], ["/data", "Data Quality & Coverage"]]],
  ["Research", [["/learning", "Learning Brain"], ["/agents", "AI Research Agents"], ["/factory", "Strategy Factory"], ["/code", "Strategy Code"], ["/memory", "Experiment Memory"], ["/chat", "AI Analyst Chat"]]],
  ["Testing", [["/backtest", "Backtesting Studio"], ["/replay", "Trade Replay"], ["/walkforward", "Walk-Forward Testing"], ["/compare", "Strategy Comparison"], ["/portfolio", "Portfolio Simulation"]]],
  ["Trading", [["/paper", "Paper Trading Terminal"], ["/brokers", "Broker Connections"], ["/risk", "Risk Control Center"]]],
  ["Operations", [["/system", "System Monitoring"], ["/settings", "Settings & Audit Logs"]]],
];

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

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [open, setOpen] = useState(false);
  const [locked, setLocked] = useState(false);
  const sys = useApi<any>(locked ? null : "/system", 15000);
  useEffect(() => {
    const h = () => setLocked(true);
    window.addEventListener("nseql-unauthorized", h);
    return () => window.removeEventListener("nseql-unauthorized", h);
  }, []);
  useEffect(() => setOpen(false), [path]);
  if (locked) return <Login onDone={() => { setLocked(false); window.location.reload(); }} />;
  const s = sys.data;
  return (
    <div className="shell">
      <aside className={`side ${open ? "open" : ""}`}>
        <div className="brand">NSE AI Quant Lab<small>research &amp; paper trading</small></div>
        <nav className="nav">
          {NAV.map(([g, items]) => (
            <div key={g}><div className="navg">{g}</div>
              {items.map(([href, label]) => <Link key={href} href={href} className={path === href ? "on" : ""}>{label}</Link>)}</div>))}
        </nav>
      </aside>
      <main className="main">
        <div className="top" style={{ marginBottom: 10 }}>
          <button className="menu" onClick={() => setOpen(!open)} aria-label="Menu">☰ Menu</button>
          <span className="sp" />
          {sys.error && <Badge s="API unreachable" tone="crit" />}
          {s && <>
            <Badge s={s.live_trading_enabled ? "LIVE FLAG ON" : "live trading off"} tone={s.live_trading_enabled ? "crit" : "good"} />
            <Badge s={s.kill_switch.engaged ? "kill switch ENGAGED" : "kill switch clear"} tone={s.kill_switch.engaged ? "crit" : "good"} />
            {s.counts.jobs_running > 0 && <Badge s={`${s.counts.jobs_running} job running`} tone="info" />}
            <span className="dim mono">v{s.code_version}</span>
          </>}
        </div>
        {children}
      </main>
    </div>
  );
}
