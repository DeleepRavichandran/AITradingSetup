"use client";
/* My trading: the simple view. Plain language, big numbers, no jargon. Everything comes from /api/today.
   Paper (simulated) trading only. Technical detail lives on the other pages. */
import Link from "next/link";
import { Donut, LineChart, Ring } from "@/components/charts";
import { SessionPanel, sessionState, whatHappened } from "@/components/session";
import { Err, Prov, Skeleton } from "@/components/ui";
import { inr, num, useApi } from "@/lib/api";

const LIGHT: Record<string, [string, string]> = {
  ok: ["var(--good)", "Bot is working"], waiting: ["var(--data)", "Bot is waiting"], stopped: ["var(--crit)", "Bot is paused"],
  stale: ["var(--warn)", "Bot needs attention"], off: ["var(--muted)", "Bot is off"], error: ["var(--crit)", "Bot could not start"],
};
/* small line icons, drawn inline (no external assets) */
const ICONS: Record<string, string> = {
  wallet: "M3 7h15a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3zM3 7l11-4 2 4M17 13.5h.01",
  chart: "M3 20h18M5 16l4-5 4 3 6-8",
  pie: "M12 3v9h9A9 9 0 1 1 12 3zM15 3.3A9 9 0 0 1 20.7 9H15z",
  bars: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zm0-5a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0-3.5h.01",
  bag: "M5 8h14l-1 12H6zM9 8V6a3 3 0 0 1 6 0v2",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
};
const Icon = ({ n, color = "var(--data)" }: { n: string; color?: string }) => (
  <span aria-hidden="true" style={{ display: "inline-grid", placeItems: "center", width: 30, height: 30, borderRadius: 8, marginRight: 10, verticalAlign: "middle",
    background: `color-mix(in srgb, ${color} 18%, transparent)` }}>
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d={ICONS[n]} /></svg>
  </span>
);
const H = ({ id, icon, color, children }: { id: string; icon: string; color?: string; children: React.ReactNode }) => (
  <h2 id={id} style={{ display: "flex", alignItems: "center" }}><Icon n={icon} color={color} />{children}</h2>
);
const signed = (x: number | null | undefined) => (x == null ? "—" : `${x > 0 ? "+" : ""}${inr(x)}`);
const tone = (x: number | null | undefined) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");

export default function Today() {
  const t = useApi<any>("/today", 10000);
  const d = t.data;
  const [color, label] = LIGHT[d?.bot?.state] || LIGHT.off;
  const a = d?.account;
  return (
    <div style={{ maxWidth: 980 }}>
      <div className="top"><h1>My Trading</h1></div>
      <p className="sub">Your practice account, in plain words. Every trade here uses pretend money (paper trading); where the prices come from is shown next to the light below.</p>
      <Err e={t.error} />
      {!d && !t.error && <><p className="banner info" role="status" data-testid="today-connecting">Connecting to the lab…</p><Skeleton lines={4} /></>}
      {d && <>
        <section className="panel" style={{ marginBottom: 14, flexDirection: "row", alignItems: "center", gap: 14 }} aria-live="polite" data-testid="bot-light">
          <span aria-hidden="true" style={{ width: 18, height: 18, borderRadius: "50%", background: color, boxShadow: `0 0 0 6px color-mix(in srgb, ${color} 20%, transparent)`, flex: "none" }} />
          <div><div style={{ font: "600 20px var(--cond)" }}>{label}</div><div className="ink2">{d.bot.message}</div></div>
          <span style={{ flex: 1 }} />
          <span className="dim" style={{ fontSize: 13, textAlign: "right" }}>Prices: {d.data_source?.label}<br />Market: {d.market.state}</span>
        </section>

        {a && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="money-h">
          <H id="money-h" icon="wallet" color="var(--s3)">Your money</H>
          <div className="metrics">
            <div className="metric"><div className="k">Account value now</div><div className="v">{inr(a.value_now)}</div><div className="h">started with {inr(a.starting_capital)}</div></div>
            <div className="metric" title={a.windows?.pnl_today}><div className="k">P&amp;L today</div><div className={`v ${tone(a.today)}`} data-testid="today-pnl">{signed(a.today)}</div><div className="h">{a.today_trades} trade{a.today_trades === 1 ? "" : "s"} · charges {inr(a.today_charges)}</div></div>
            <div className="metric" title={a.windows?.pnl_since_start}><div className="k">Since the start</div><div className={`v ${tone(a.since_start)}`} data-testid="since-pnl">{signed(a.since_start)}</div><div className="h">from {inr(a.starting_capital)}</div></div>
            <div className="metric"><div className="k">Free cash</div><div className="v">{inr(a.cash_free)}</div><div className="h">{inr(a.invested)} in stocks</div></div>
          </div>
          <p className="ink2" style={{ margin: "10px 0 0" }} data-testid="today-what"><b>{sessionState(d, t.error).label}.</b> {whatHappened(d)}</p>
          {a.halted_today && <p className="down" style={{ marginTop: 10 }}>Today's loss limit was reached, so the bot stopped buying for the rest of the day. It can still sell.</p>}
        </section>}

        {d.charts && <Charts c={d.charts} ds={d.data_source} />}

        {d.started && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="hold-h">
          <H id="hold-h" icon="bag" color="var(--s4)">What you own now ({d.holdings.length})</H>
          {d.holdings.length ? <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))" }}>
            {d.holdings.map((h: any) => (
              <Link key={h.config_id} href={`/universe?config=${h.config_id}`} className="status-cell" style={{ textDecoration: "none", color: "inherit", display: "block" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><b style={{ font: "600 18px var(--cond)" }}>{h.symbol}</b>
                  <span className={tone(h.unrealised_inr)} style={{ fontWeight: 600 }}>{signed(h.unrealised_inr)}</span></div>
                <div className="ink2" style={{ fontSize: 14 }}>{h.qty} share{h.qty === 1 ? "" : "s"} · bought at {inr(h.entry_price, 2)} · now {inr(h.last_price, 2)}</div>
                <div className="dim" style={{ fontSize: 13 }}>{h.rule} · {h.horizon === "intraday" ? "sold by 3:15 PM today" : "can be held overnight"}{h.exit_pending ? " · selling now" : ""}</div>
                <div className="dim" style={{ fontSize: 13 }}>Safety stop at {inr(h.stop_price, 2)}</div>
              </Link>))}
          </div> : <p className="ink2" style={{ margin: 0 }}>Nothing right now. The bot buys only when a strategy's rule is met and the portfolio limits allow it.</p>}
        </section>}

        {d.started && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="trades-h">
          <H id="trades-h" icon="list" color="var(--s7)">Latest trades</H>
          {d.recent_trades.length ? <ul className="list">{d.recent_trades.map((x: any, i: number) => (
            <li key={i}>
              <span className="t"><span className="dim num">{x.time.slice(11)} {x.time.slice(0, 10) !== new Date().toISOString().slice(0, 10) ? `(${x.time.slice(5, 10)})` : ""}</span>{" "}
                <b>{x.action}</b> {x.qty} {x.symbol} at {inr(x.price, 2)}
                {x.profit != null && <> · <span className={tone(x.profit)}>{x.profit >= 0 ? "profit" : "loss"} {inr(Math.abs(x.profit))}</span></>}</span>
              <div className="d">Strategy: {x.strategy} ({x.horizon}) · charges {inr(x.charges, 2)}</div>
            </li>))}</ul>
            : <p className="ink2" style={{ margin: 0 }}>No trades yet today.</p>}
          <details style={{ marginTop: 10 }} data-testid="today-session"><summary>Why buys were or were not taken this session</summary>
            <SessionPanel s={d.session} /></details>
        </section>}

        <p className="dim" style={{ fontSize: 13 }}>Want the details? <Link href="/screening">every decision and fill</Link> · <Link href="/universe">which strategies work</Link> · <Link href="/command">Command Center</Link> · ask Vega with Ctrl K.
          Real-money orders are switched off.</p>
      </>}
    </div>
  );
}

const FAMILY: Record<string, string> = { cross: "MA cross", breakout: "Breakout", rsi: "RSI", zscore: "Z-score", bollinger: "Bollinger", macd: "MACD",
  momentum: "Momentum", volume: "Volume", vwap: "VWAP", orb: "Opening range", gap: "Gap", prevday: "Prev. day" };

function Charts({ c, ds }: { c: any; ds: any }) {
  const curve = c.equity_curve || [];
  const last = curve.length ? curve[curve.length - 1].v : null;
  const up = last != null && last >= c.day_open_value;
  const maxAbs = Math.max(1, ...c.pnl_by_family.map((x: any) => Math.abs(x.value)));
  const maxHour = Math.max(1, ...c.trades_by_hour.map((x: any) => x.buys + x.sells));
  const prov = <Prov source="this bot's paper ledger" period={c.session} data={ds} />;
  return (
    <>
      <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="curve-h" data-testid="chart-curve">
        <H id="curve-h" icon="chart" color={up ? "var(--good)" : "var(--crit)"}>Account value through the day</H>
        {curve.length > 1 ? <>
          <p className="ink2" style={{ margin: "0 0 6px" }}>Opened at {inr(c.day_open_value)} · now <b className={up ? "up" : "down"}>{inr(last)}</b> ({signed(last - c.day_open_value)})</p>
          <div style={{ ["--s1" as any]: up ? "var(--good)" : "var(--crit)" }}>
            <LineChart x={curve.map((p: any) => p.t)} series={[{ name: "account ₹", values: curve.map((p: any) => p.v) }]} height={200} area fmt={(v) => inr(v)} />
          </div>
        </> : <p className="dim" style={{ margin: 0 }}>The line appears after the first minutes of trading.</p>}
        {prov}
      </section>
      <div className="grid g2" style={{ marginBottom: 14 }}>
        <section className="panel" aria-labelledby="alloc-h" data-testid="chart-alloc">
          <H id="alloc-h" icon="pie" color="var(--s5)">Where your money is</H>
          <Donut items={c.allocation} fmt={(v) => inr(v)} label="Money split between free cash and each stock held"
            centre={<><b style={{ font: "600 15px var(--cond)" }}>{c.allocation.length - 1}</b><br /><span className="dim">stock{c.allocation.length - 1 === 1 ? "" : "s"}</span></>} />
          {prov}
        </section>
        <section className="panel" aria-labelledby="win-h" data-testid="chart-wins">
          <H id="win-h" icon="target" color="var(--s2)">Winners vs losers today</H>
          <div style={{ display: "flex", gap: 18, alignItems: "center" }}>
            <Ring part={c.wins} total={c.wins + c.losses} label={`${c.wins} winning and ${c.losses} losing exits today`} />
            <div><div><span className="up" style={{ font: "600 22px var(--cond)" }}>{c.wins}</span> <span className="ink2">winning sells</span></div>
              <div><span className="down" style={{ font: "600 22px var(--cond)" }}>{c.losses}</span> <span className="ink2">losing sells</span></div>
              <div className="dim" style={{ fontSize: 13 }}>after charges</div></div>
          </div>
          {prov}
        </section>
      </div>
      <div className="grid g2" style={{ marginBottom: 14 }}>
        <section className="panel" aria-labelledby="fam-h" data-testid="chart-family">
          <H id="fam-h" icon="bars" color="var(--s1)">Profit or loss by strategy type</H>
          {c.pnl_by_family.length ? <div style={{ display: "grid", gap: 8 }}>
            {c.pnl_by_family.map((x: any) => (
              <div key={x.label} style={{ display: "grid", gridTemplateColumns: "108px 1fr 76px", gap: 8, alignItems: "center" }}>
                <span className="ink2" style={{ fontSize: 14 }}>{FAMILY[x.label] || x.label}</span>
                <span style={{ position: "relative", height: 16, background: "var(--raised)", borderRadius: 4 }}>
                  <span style={{ position: "absolute", left: "50%", top: -3, bottom: -3, borderLeft: "1px solid var(--axis)" }} />
                  <span style={{ position: "absolute", top: 0, height: 16, borderRadius: 4, background: x.value >= 0 ? "var(--good)" : "var(--crit)",
                    left: x.value >= 0 ? "50%" : `${50 - (50 * Math.abs(x.value)) / maxAbs}%`, width: `${(50 * Math.abs(x.value)) / maxAbs}%` }} />
                </span>
                <span className={`num ${x.value >= 0 ? "up" : "down"}`} style={{ textAlign: "right" }}>{signed(x.value)}</span>
              </div>))}
          </div> : <p className="dim" style={{ margin: 0 }}>No completed sells yet today.</p>}
          {prov}
        </section>
        <section className="panel" aria-labelledby="hour-h" data-testid="chart-hours">
          <H id="hour-h" icon="clock" color="var(--s6)">Trades by hour</H>
          {c.trades_by_hour.length ? <>
            <div style={{ display: "flex", alignItems: "flex-end", gap: 10, height: 130, padding: "0 4px" }}>
              {c.trades_by_hour.map((x: any) => (
                <div key={x.hour} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 4, minWidth: 0 }} title={`${x.hour}: ${x.buys} bought, ${x.sells} sold`}>
                  <div style={{ width: "100%", maxWidth: 36, display: "flex", flexDirection: "column", justifyContent: "flex-end", height: 104 }}>
                    <div style={{ height: `${(100 * x.sells) / maxHour}%`, background: "var(--s1)", borderRadius: "4px 4px 0 0" }} />
                    <div style={{ height: `${(100 * x.buys) / maxHour}%`, background: "var(--good)", borderRadius: x.sells ? 0 : "4px 4px 0 0" }} />
                  </div>
                  <span className="dim" style={{ fontSize: 11.5 }}>{x.hour.slice(0, 2)}</span>
                </div>))}
            </div>
            <div className="legend"><span><i style={{ background: "var(--good)" }} />bought</span><span><i style={{ background: "var(--s1)" }} />sold</span></div>
          </> : <p className="dim" style={{ margin: 0 }}>No trades yet today.</p>}
          {prov}
        </section>
      </div>
      {c.rca && !c.rca.error && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="rca-h" data-testid="rca">
        <H id="rca-h" icon="target" color="var(--s5)">Why trades won or lost</H>
        {(c.rca.by_cause || []).length ? <>
          <p className="ink2" style={{ margin: "0 0 8px" }}>Every completed trade gets a root cause: <b className="up">{c.rca.won} won</b>, <b className="down">{c.rca.lost} lost</b>.</p>
          <ul className="list" style={{ margin: 0 }}>
            {c.rca.by_cause.map((g: any) => (
              <li key={g.cause} style={{ display: "flex", gap: 10, padding: "5px 0", alignItems: "baseline", flexWrap: "wrap" }}>
                <i style={{ width: 10, height: 10, borderRadius: 3, background: g.outcome === "won" ? "var(--good)" : "var(--crit)", display: "inline-block", flex: "none" }} />
                <span style={{ flex: 1, minWidth: 200 }}>{g.cause}</span><span className="num">{g.trades} trades</span>
                <span className={`num ${g.net_inr >= 0 ? "up" : "down"}`} style={{ minWidth: 80, textAlign: "right" }}>{signed(g.net_inr)}</span>
              </li>))}
          </ul>
          <details style={{ marginTop: 10 }}><summary>Latest trades with their cause</summary>
            <ul className="list" style={{ margin: "6px 0 0" }}>
              {c.rca.latest.map((t: any, k: number) => (
                <li key={k} style={{ padding: "6px 0" }}><span className="dim num">{t.time}</span> <b>{t.symbol}</b> {t.summary}
                  {t.causes?.length ? <div className="dim" style={{ fontSize: 13 }}>Also: {t.causes.join("; ")}</div> : null}</li>))}
            </ul>
          </details>
        </> : <p className="dim" style={{ margin: 0 }}>No completed trades yet today. Each one gets its root cause as soon as it is sold.</p>}
        <p className="dim" style={{ fontSize: 12.5, margin: "8px 0 0" }}>"Recovered after the sale" looks at prices after the exit: hindsight, used for learning only, never for trading.</p>
        {prov}
      </section>}
      <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="busy-h" data-testid="chart-busy">
        <H id="busy-h" icon="bars" color="var(--s8)">Busiest strategies today</H>
        {(c.most_active || []).length ? <ul className="list" style={{ margin: 0 }}>
          {c.most_active.map((m: any) => (
            <li key={m.strategy + m.symbol} style={{ display: "flex", gap: 10, alignItems: "baseline", padding: "6px 0", flexWrap: "wrap" }}>
              <b style={{ minWidth: 92 }}>{m.symbol}</b><span className="ink2" style={{ flex: 1, minWidth: 160 }}>{m.strategy}</span>
              <span className="num">{m.trades} trades</span>
              <span className={`num ${m.profit >= 0 ? "up" : "down"}`} style={{ minWidth: 80, textAlign: "right" }}>{signed(m.profit)}</span>
            </li>))}
        </ul> : <p className="dim" style={{ margin: 0 }}>No trades yet today.</p>}
        <p className="ink2" style={{ margin: "10px 0 0" }} data-testid="limited">
          Trading rules {c.rules_version}.{" "}
          {(c.limited || []).length
            ? <>Buys held back today: {c.limited.map((x: any) => `${x.label} ${x.value.toLocaleString("en-IN")}`).join(" · ")}.</>
            : "No buys were held back by the trading limits today."}
        </p>
        {prov}
      </section>
    </>
  );
}
