"use client";
/* My trading: the simple view. Plain language, big numbers, no jargon. Everything comes from /api/today.
   Paper (simulated) trading only. Technical detail lives on the other pages. */
import Link from "next/link";
import { Err, Skeleton } from "@/components/ui";
import { inr, num, useApi } from "@/lib/api";

const LIGHT: Record<string, [string, string]> = {
  ok: ["var(--good)", "Bot is working"], waiting: ["var(--data)", "Bot is waiting"], stopped: ["var(--crit)", "Bot is paused"],
  stale: ["var(--warn)", "Bot needs attention"], off: ["var(--muted)", "Bot is off"], error: ["var(--crit)", "Bot could not start"],
};
const signed = (x: number | null | undefined) => (x == null ? "—" : `${x > 0 ? "+" : ""}${inr(x)}`);
const tone = (x: number | null | undefined) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");

export default function Today() {
  const t = useApi<any>("/today", 10000);
  const d = t.data;
  const [color, label] = LIGHT[d?.bot?.state] || LIGHT.off;
  const a = d?.account;
  return (
    <div style={{ maxWidth: 980 }}>
      <div className="top"><h1>My trading</h1></div>
      <p className="sub">Your practice account, in plain words. Every trade here uses pretend money (paper trading); where the prices come from is shown next to the light below.</p>
      <Err e={t.error} />
      {!d && !t.error && <Skeleton lines={4} />}
      {d && <>
        <section className="panel" style={{ marginBottom: 14, flexDirection: "row", alignItems: "center", gap: 14 }} aria-live="polite" data-testid="bot-light">
          <span aria-hidden="true" style={{ width: 18, height: 18, borderRadius: "50%", background: color, boxShadow: `0 0 0 6px color-mix(in srgb, ${color} 20%, transparent)`, flex: "none" }} />
          <div><div style={{ font: "600 20px var(--cond)" }}>{label}</div><div className="ink2">{d.bot.message}</div></div>
          <span style={{ flex: 1 }} />
          <span className="dim" style={{ fontSize: 13, textAlign: "right" }}>Prices: {d.data_source?.label}<br />Market: {d.market.state}</span>
        </section>

        {a && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="money-h">
          <h2 id="money-h">Your money</h2>
          <div className="metrics">
            <div className="metric"><div className="k">Account value now</div><div className="v">{inr(a.value_now)}</div><div className="h">started with {inr(a.starting_capital)}</div></div>
            <div className="metric"><div className="k">Today</div><div className={`v ${tone(a.today)}`}>{signed(a.today)}</div><div className="h">{a.today_trades} trade{a.today_trades === 1 ? "" : "s"} · charges {inr(a.today_charges)}</div></div>
            <div className="metric"><div className="k">Since the start</div><div className={`v ${tone(a.since_start)}`}>{signed(a.since_start)}</div></div>
            <div className="metric"><div className="k">Free cash</div><div className="v">{inr(a.cash_free)}</div><div className="h">{inr(a.invested)} in stocks</div></div>
          </div>
          {a.halted_today && <p className="down" style={{ marginTop: 10 }}>Today's loss limit was reached, so the bot stopped buying for the rest of the day. It can still sell.</p>}
        </section>}

        {d.started && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="hold-h">
          <h2 id="hold-h">What you own now ({d.holdings.length})</h2>
          {d.holdings.length ? <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))" }}>
            {d.holdings.map((h: any) => (
              <Link key={h.config_id} href={`/universe?config=${h.config_id}`} className="status-cell" style={{ textDecoration: "none", color: "inherit", display: "block" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><b style={{ font: "600 18px var(--cond)" }}>{h.symbol}</b>
                  <span className={tone(h.unrealised_inr)} style={{ fontWeight: 600 }}>{signed(h.unrealised_inr)}</span></div>
                <div className="ink2" style={{ fontSize: 14 }}>{h.qty} share{h.qty === 1 ? "" : "s"} · bought at {inr(h.entry_price, 2)} · now {inr(h.last_price, 2)}</div>
                <div className="dim" style={{ fontSize: 13 }}>{h.rule} · {h.horizon === "intraday" ? "sold by 3:15 PM today" : "can be held overnight"}{h.exit_pending ? " · selling now" : ""}</div>
                <div className="dim" style={{ fontSize: 13 }}>Safety stop at {inr(h.stop_price, 2)}</div>
              </Link>))}
          </div> : <p className="ink2" style={{ margin: 0 }}>Nothing right now. The bot buys only when a strategy's rule is met and the safety limits allow it.</p>}
        </section>}

        {d.started && <section className="panel" style={{ marginBottom: 14 }} aria-labelledby="trades-h">
          <h2 id="trades-h">Latest trades</h2>
          {d.recent_trades.length ? <ul className="list">{d.recent_trades.map((x: any, i: number) => (
            <li key={i}>
              <span className="t"><span className="dim num">{x.time.slice(11)} {x.time.slice(0, 10) !== new Date().toISOString().slice(0, 10) ? `(${x.time.slice(5, 10)})` : ""}</span>{" "}
                <b>{x.action}</b> {x.qty} {x.symbol} at {inr(x.price, 2)}
                {x.profit != null && <> · <span className={tone(x.profit)}>{x.profit >= 0 ? "profit" : "loss"} {inr(Math.abs(x.profit))}</span></>}</span>
              <div className="d">Strategy: {x.strategy} ({x.horizon}) · charges {inr(x.charges, 2)}</div>
            </li>))}</ul>
            : <p className="ink2" style={{ margin: 0 }}>No trades yet today.</p>}
          <p className="dim" style={{ fontSize: 13, marginTop: 10 }}>Today the strategies wanted to buy {num(d.signals_today.accepted + d.signals_today.not_taken, 0)} times; {num(d.signals_today.accepted, 0)} got money,
            the rest were skipped by the safety limits (₹ per trade, per stock and daily loss).</p>
        </section>}

        <p className="dim" style={{ fontSize: 13 }}>Want the details? <Link href="/screening">every decision and fill</Link> · <Link href="/universe">which strategies work</Link> · <Link href="/">Command Center</Link> · ask Vega with Ctrl K.
          Real-money orders are switched off.</p>
      </>}
    </div>
  );
}
