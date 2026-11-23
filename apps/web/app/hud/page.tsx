"use client";
/* Mission control: the whole bot on one instrument panel. Every figure comes from the existing APIs
   (/today, /contest, /market/pulse, /news/recent); nothing is invented. Paper (simulated) trading only. */
import Link from "next/link";
import { useEffect, useState } from "react";
import { LineChart } from "@/components/charts";
import { SessionPanel, sessionState, whatHappened } from "@/components/session";
import { inr, useApi } from "@/lib/api";
import s from "./hud.module.css";

const C = 200;                                    // core centre (viewBox 400 x 400)
const polar = (r: number, a: number) => [C + r * Math.sin(a), C - r * Math.cos(a)];
function arc(r: number, a0: number, a1: number) {
  if (Math.abs(a1 - a0) < 1e-4) return "";
  const [x0, y0] = polar(r, a0), [x1, y1] = polar(r, a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${Math.abs(a1 - a0) > Math.PI ? 1 : 0} ${a1 > a0 ? 1 : 0} ${x1} ${y1}`;
}
const signed = (x: number) => `${x >= 0 ? "+" : "−"}${inr(Math.abs(x))}`;
const STATUS: Record<string, [string, string]> = {
  ok: ["var(--green)", "Working"], waiting: ["var(--cyan)", "Waiting for the market"], stopped: ["var(--red)", "Paused by the kill switch"],
  stale: ["var(--amber)", "No fresh prices"], off: ["var(--dim)", "Off"], error: ["var(--red)", "Could not start"],
};

function useIst() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => { setNow(new Date()); const t = setInterval(() => setNow(new Date()), 30000); return () => clearInterval(t); }, []);
  if (!now) return { clock: "", sessionShare: 0 };
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .formatToParts(now).map((x) => [x.type, x.value]));
  const mins = Number(p.hour) * 60 + Number(p.minute);
  return { clock: `${p.hour}:${p.minute} IST`, sessionShare: Math.min(1, Math.max(0, (mins - 555) / 375)) };
}

export default function MissionControl() {
  const t = useApi<any>("/today", 15000);
  const k = useApi<any>("/contest", 60000);
  const p = useApi<any>("/market/pulse?limit=1", 60000);
  const n = useApi<any>("/news/recent?limit=8", 120000);
  const { clock, sessionShare } = useIst();
  const d = t.data, a = d?.account, ch = d?.charts;
  const st = STATUS[d?.bot?.state || "off"] || STATUS.off;
  const dayRet = a && ch?.day_open_value ? (a.value_now - ch.day_open_value) / ch.day_open_value : 0;
  const exposure = a && a.value_now ? Math.min(1, Math.max(0, a.invested / a.value_now)) : 0;
  const fw = k.data?.forward && !k.data.forward.error ? k.data.forward : null;
  const pulse = p.data?.pulses?.[0];
  const curve = ch?.equity_curve || [];
  const trades = ((ch?.rca?.latest || []) as any[]).map((r) => ({ time: r.time, text: `${r.symbol}: ${r.summary}`, tone: r.outcome === "won" ? s.up : s.down }));
  const heads = ((n.data?.items || []) as any[]).slice(0, 8).map((h) => ({ time: String(h.first_seen || "").slice(11, 16), text: h.title }));

  return (
    <div className={s.deck}>
      <div className={s.top}>
        <h1 className={s.title}>Mission <span>Control</span></h1>
        <div className={s.meta}>
          <span className={s.chip}><b>{clock || "—"}</b></span>
          <span className={s.chip}>Market <b>{d?.market?.state || "—"}</b></span>
          <span className={s.chip} data-testid="hud-source">Prices: <b>{d?.data_source?.label || "no market data"}</b></span>
          <span className={s.chip}>Real-money orders <b>off</b></span>
        </div>
      </div>

      {(() => { const st2 = sessionState(d, t.error); return (
        <div className={s.state} data-tone={st2.tone} role="status" data-testid="hud-state">
          <span className={s.dot} style={{ color: `var(--${st2.tone === "good" ? "green" : st2.tone === "warn" ? "amber" : st2.tone === "crit" ? "red" : "cyan"})` }} aria-hidden="true" />
          <span><b>{st2.label}</b> <span className={s.k}>{st2.text}</span></span>
        </div>); })()}

      <div className={s.kpis}>
        <section className={`${s.panel} ${s.kpi}`} aria-label="Account value"><span className={s.k}>Account value</span>
          <span className={s.big} data-testid="kpi-value">{a ? inr(a.value_now) : "—"}</span></section>
        <section className={`${s.panel} ${s.kpi}`} aria-label="P&L today"><span className={s.k}>P&amp;L today</span>
          <span className={`${s.big} ${a && a.today >= 0 ? s.up : s.down}`} data-testid="kpi-today" title={a?.windows?.pnl_today}>{a ? signed(a.today) : "—"}</span></section>
        <section className={`${s.panel} ${s.kpi}`} aria-label="P&L since inception"><span className={s.k}>Since inception{a?.starting_capital ? ` (${inr(a.starting_capital)})` : ""}</span>
          <span className={`${s.big} ${a && (a.since_start ?? 0) >= 0 ? s.up : s.down}`} data-testid="kpi-since" title={a?.windows?.pnl_since_start}>{a?.since_start == null ? "—" : signed(a.since_start)}</span></section>
        <section className={`${s.panel} ${s.kpi}`} aria-label="Exposure"><span className={s.k}>Exposure</span>
          <span className={s.big} data-testid="kpi-exposure">{a?.exposure == null ? "—" : `${(a.exposure * 100).toFixed(1)}%`}</span></section>
        <section className={`${s.panel} ${s.kpi}`} aria-labelledby="hud-what"><h2 id="hud-what" className={s.h}>What happened this session?</h2>
          <p className={s.happen} data-testid="hud-what">{d ? whatHappened(d) : "—"}</p></section>
      </div>

      <div className={s.grid}>
        <div className={s.col}>
          <section className={s.panel} aria-labelledby="hud-bot">
            <h2 id="hud-bot" className={s.h}>Bot</h2>
            <div className={s.status}>
              <span className={s.dot} style={{ color: st[0] }} aria-hidden="true" />
              <div><div className={s.v} data-testid="hud-bot">{st[1]}</div><div className={s.k}>{d?.bot?.message || "Waiting for the API…"}</div></div>
            </div>
          </section>
          <section className={s.panel} aria-labelledby="hud-session" data-testid="hud-session">
            <h2 id="hud-session" className={s.h}>Signals, orders and fills <em>{d?.session?.day || ""}</em></h2>
            <SessionPanel s={d?.session} className={s.sp} rowClass={s.row} kClass={s.k} vClass={s.v} />
            {a?.halted_today && <p className={s.warn} style={{ margin: "8px 0 0", fontSize: 14 }}>Daily loss limit reached: no new buys today.</p>}
          </section>
          <section className={s.panel} aria-labelledby="hud-hold">
            <h2 id="hud-hold" className={s.h}>Holding <em>{d?.holdings?.length ?? 0}</em></h2>
            {(d?.holdings || []).slice(0, 6).map((h: any) => (
              <div key={h.config_id} className={s.row}><span><b>{h.symbol}</b> <span className={s.k}>{h.qty} @ {h.entry_price}</span></span>
                <span className={`${s.v} ${h.unrealised_inr >= 0 ? s.up : s.down}`}>{signed(h.unrealised_inr)}</span></div>))}
            {!d?.holdings?.length && <p className={s.dim} style={{ margin: 0 }}>Nothing held right now.</p>}
          </section>
        </div>

        <section className={`${s.panel} ${s.core}`} aria-labelledby="hud-core" data-testid="hud-core">
          <h2 id="hud-core" className={s.h} style={{ width: "100%" }}>Account core</h2>
          <svg viewBox="0 0 400 400" role="img"
            aria-label={a ? `Account value ${inr(a.value_now)}, ${(dayRet * 100).toFixed(2)}% today, ${Math.round(exposure * 100)}% invested, session ${Math.round(sessionShare * 100)}% done` : "No account data"}>
            <defs>
              <radialGradient id="glow"><stop offset="0" stopColor="#5cc8ff" stopOpacity="0.22" /><stop offset="1" stopColor="#5cc8ff" stopOpacity="0" /></radialGradient>
              <linearGradient id="sw" x1="0" x2="1"><stop offset="0" stopColor="#5cc8ff" stopOpacity="0" /><stop offset="1" stopColor="#5cc8ff" stopOpacity="0.5" /></linearGradient>
            </defs>
            <circle cx={C} cy={C} r={196} fill="url(#glow)" />
            {Array.from({ length: 75 }, (_, i) => {
              const ang = (i / 75) * 2 * Math.PI, [x0, y0] = polar(186, ang), [x1, y1] = polar(i % 5 ? 191 : 196, ang);
              return <line key={i} x1={x0} y1={y0} x2={x1} y2={y1} stroke="#5cc8ff" strokeOpacity={i / 75 <= sessionShare ? 0.85 : 0.18} strokeWidth={i % 5 ? 1 : 2} />;
            })}
            <circle cx={C} cy={C} r={160} fill="none" stroke="#5cc8ff" strokeOpacity="0.12" strokeWidth="14" />
            <path d={arc(160, 0, Math.max(-Math.PI, Math.min(Math.PI, (dayRet / 0.03) * Math.PI)))} fill="none"
              stroke={dayRet >= 0 ? "#39d98a" : "#ff6b85"} strokeWidth="14" strokeLinecap="round" />
            <circle cx={C} cy={C} r={132} fill="none" stroke="#ffb547" strokeOpacity="0.12" strokeWidth="8" />
            <path d={arc(132, 0, exposure * 2 * Math.PI * 0.9999)} fill="none" stroke="#ffb547" strokeWidth="8" strokeLinecap="round" />
            <g className={s.sweep}><path d={`M ${C} ${C} L ${C} ${C - 118} A 118 118 0 0 1 ${polar(118, 0.5)[0]} ${polar(118, 0.5)[1]} Z`} fill="url(#sw)" opacity="0.35" /></g>
            <circle cx={C} cy={C} r={112} fill="#08101d" stroke="#5cc8ff" strokeOpacity="0.25" />
            <text x={C} y={C - 26} textAnchor="middle" fill="#8ea3bf" fontSize="14">Account value</text>
            <text x={C} y={C + 12} textAnchor="middle" fill="#dce7f5" style={{ font: "600 36px var(--cond)" }}>{a ? inr(a.value_now) : "—"}</text>
            <text x={C} y={C + 40} textAnchor="middle" fill={dayRet >= 0 ? "#39d98a" : "#ff6b85"} style={{ font: "600 18px var(--cond)" }}>
              {a ? `${dayRet >= 0 ? "+" : "−"}${Math.abs(dayRet * 100).toFixed(2)}% today` : ""}</text>
            <text x={C} y={C + 62} textAnchor="middle" fill="#8ea3bf" fontSize="12.5">{a?.starting_capital ? `started with ${inr(a.starting_capital)}` : ""}</text>
          </svg>
          <div className={s.legend}>
            <span><i style={{ background: "#5cc8ff" }} />session done</span>
            <span><i style={{ background: dayRet >= 0 ? "#39d98a" : "#ff6b85" }} />today (full ring = ±3%)</span>
            <span><i style={{ background: "#ffb547" }} />invested share</span>
          </div>
        </section>

        <div className={s.col}>
          <section className={s.panel} aria-labelledby="hud-contest" data-testid="hud-contest">
            <h2 id="hud-contest" className={s.h}>Challenger contest <Link href="/contest">details</Link></h2>
            {fw ? <>
              <div className={s.v} style={{ color: fw.verdict.status === "challenger ahead" ? "var(--green)" : fw.verdict.status === "incumbent ahead" ? "var(--red)" : "var(--amber)" }}>
                {fw.verdict.status}</div>
              <div className={s.k} style={{ marginBottom: 6 }}>{fw.challenger.version} vs {fw.incumbent.version}</div>
              <div className={s.k}>Sessions {fw.sessions.length} of {fw.verdict.rules.min_sessions}</div>
              <div className={s.bar}><span style={{ width: `${Math.min(100, (100 * fw.sessions.length) / fw.verdict.rules.min_sessions)}%` }} /></div>
              <div className={s.k}>Challenger trades {fw.challenger.trades_closed} of {fw.verdict.rules.min_challenger_trades}</div>
              <div className={s.bar}><span style={{ width: `${Math.min(100, (100 * fw.challenger.trades_closed) / fw.verdict.rules.min_challenger_trades)}%` }} /></div>
              <div className={s.row}><span className={s.k}>Incumbent net</span><span className={`${s.v} ${fw.incumbent.net_pnl_inr >= 0 ? s.up : s.down}`}>{signed(fw.incumbent.net_pnl_inr)}</span></div>
              <div className={s.row}><span className={s.k}>Challenger net</span><span className={`${s.v} ${fw.challenger.net_pnl_inr >= 0 ? s.up : s.down}`}>{signed(fw.challenger.net_pnl_inr)}</span></div>
            </> : <p className={s.dim} style={{ margin: 0 }}>The forward contest starts with live screening.</p>}
          </section>
          <section className={s.panel} aria-labelledby="hud-breadth" data-testid="hud-breadth">
            <h2 id="hud-breadth" className={s.h}>Market breadth <em>{pulse ? `${String(pulse.at).slice(11, 16)}` : ""}</em></h2>
            {pulse ? <>
              <div className={s.split} aria-hidden="true">
                <span style={{ width: `${(100 * pulse.breadth.up) / Math.max(1, pulse.breadth.symbols)}%`, background: "var(--green)" }} />
                <span style={{ width: `${(100 * pulse.breadth.down) / Math.max(1, pulse.breadth.symbols)}%`, background: "var(--red)" }} />
              </div>
              <div className={s.k}><span className={s.up}>{pulse.breadth.up} up</span> · <span className={s.down}>{pulse.breadth.down} down</span> of {pulse.breadth.symbols} watchlist stocks</div>
              {[...(pulse.top_gainers || []).slice(0, 2), ...(pulse.top_losers || []).slice(0, 2)].map((m: any) => (
                <div key={m[0]} className={s.row}><span>{m[0]}</span><span className={`${s.v} ${m[1] >= 0 ? s.up : s.down}`}>{m[1] >= 0 ? "+" : ""}{m[1].toFixed(2)}%</span></div>))}
            </> : <p className={s.dim} style={{ margin: 0 }}>No market pulse recorded yet.</p>}
          </section>
        </div>
      </div>

      {trades.length || curve.length > 1 ? (
        <div className={s.bottom}>
          <section className={s.panel} aria-labelledby="hud-curve" data-testid="hud-curve">
            <h2 id="hud-curve" className={s.h}>Account value through the day <em>{ch?.session || ""}</em></h2>
            {curve.length > 1 ? <LineChart x={curve.map((c: any) => c.t)} series={[{ name: "account ₹", values: curve.map((c: any) => c.v) }]} height={190} area fmt={(v) => inr(v)} />
              : <p className={s.dim} style={{ margin: 0 }}>The line appears after the first minutes of trading.</p>}
          </section>
          <section className={s.panel} aria-labelledby="hud-stream" data-testid="hud-stream">
            <h2 id="hud-stream" className={s.h}>Trading events</h2>
            {trades.length ? <ul className={s.stream}>
              {trades.map((e, i) => <li key={i}><span className={s.t}>{e.time}</span><span className={e.tone}>{e.text}</span><span /></li>)}
            </ul> : <p className={s.dim} style={{ margin: 0 }}>No completed trade yet in this session.</p>}
          </section>
        </div>
      ) : (
        <section className={s.panel} style={{ marginTop: 14 }} aria-labelledby="hud-none" data-testid="hud-no-trades">
          <h2 id="hud-none" className={s.h}>No trades in this session</h2>
          <p className={s.happen}>{d ? whatHappened(d) : "Waiting for the lab…"}</p>
          {d?.session?.day && <p style={{ margin: "8px 0 0" }}>
            <Link href={`/screening?${new URLSearchParams({ day: d.session.day, decision: "BUY", ...(d.session.eligible ? { status: "rejected" } : { warmup: "only" }) }).toString()}`}>
              Open an example decision in the ledger</Link></p>}
        </section>
      )}
      <section className={s.panel} style={{ marginTop: 14 }} aria-labelledby="hud-news" data-testid="hud-news">
        <h2 id="hud-news" className={s.h}>Headlines <em>news watcher</em></h2>
        {heads.length ? <ul className={s.stream}>
          {heads.map((e, i) => <li key={i}><span className={s.t}>{e.time}</span><span className={s.dim}>{e.text}</span><span /></li>)}
        </ul> : <p className={s.dim} style={{ margin: 0 }}>No headlines recorded yet.</p>}
      </section>
      <p className={s.src}>Source: this lab's paper ledger, the challenger contest and the news watcher. {d?.data_source?.label ? `Prices: ${d.data_source.label}.` : ""} Simulated orders only.</p>
    </div>
  );
}
