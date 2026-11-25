"use client";
/* Daily Review: the lab's own after-close analysis of each session (services/screening/daily_review.py), from /api/review.
   Plain findings first, each built from a saved number; the details below. Analysis only: nothing here trades or
   changes a rule. One day is one sample. */
import { useEffect, useState } from "react";
import { DataTable, Err, Panel, Skeleton } from "@/components/ui";
import { inr, num, pct, post, useApi } from "@/lib/api";

const STATE: Record<string, [string, string]> = {
  done: ["Ready", "var(--good)"], partial: ["Ready, with a step missing", "var(--warn)"], running: ["Running now", "var(--info, var(--accent))"],
  queued: ["Waiting for the close", "var(--muted)"], interrupted: ["Interrupted (run again)", "var(--crit)"], none: ["Not run yet", "var(--muted)"],
};
const STEP: Record<string, string> = { loss_study: "Why trades lost (loss study)", day_backtest: "Every strategy on its own (day backtest)", monte_carlo: "Trade-order luck (Monte Carlo)",
  fundamental_agent: "Fundamental agent snapshot", trade_report: "Every trade, why, and next time" };
const signed = (x: number | null | undefined, d = 2) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${num(Math.abs(x), d)}`);

export default function Review() {
  const list = useApi<any>("/review", 30000);
  const sessions: any[] = list.data?.sessions || [];
  const [pick, setPick] = useState<string>("");
  const cur = sessions.find((s) => `${s.account}/${s.day}` === pick) || sessions[0];
  const live = cur && ["running", "queued"].includes(cur.state);
  const one = useApi<any>(cur ? `/review/${encodeURIComponent(cur.account)}/${cur.day}` : null, live ? 15000 : 0);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => setMsg(null), [pick]);
  const runNow = async (force: boolean) => {
    try {
      const r = await post("/review/run", { account: cur.account, day: cur.day, force });
      setMsg(r.result === "done" ? "This review is already finished." : `Queued: ${r.note}. This page updates by itself.`);
      list.reload(); one.reload();
    } catch (e: any) { setMsg(`Could not queue: ${e.message}`); }
  };
  const rev = one.data?.review;
  const st = one.data?.status;
  return (
    <>
      <h1>Daily Review</h1>
      <p className="sub">After each session closes, the lab studies the day by itself: why trades lost, how every strategy would have done on its
        own, and how much of the drawdown was the luck of trade order. Nothing to run by hand. It never changes a rule.</p>
      {list.error && <Err e={list.error} />}
      {!list.data && !list.error && <Skeleton lines={5} />}
      {list.data && !sessions.length && (
        <Panel title="No closed session yet" id="rv-empty">
          <p className="ink2" data-testid="review-empty">The first review appears after the bot finishes a full trading day (after 15:30 IST). Holidays and weekends have
            no session and no review.</p>
        </Panel>)}
      {sessions.length > 0 && (
        <div className="seg" role="group" aria-label="Session" style={{ marginBottom: 14, flexWrap: "wrap" }}>
          {sessions.slice(0, 15).map((s) => (
            <button key={`${s.account}/${s.day}`} type="button" aria-pressed={cur === s} onClick={() => setPick(`${s.account}/${s.day}`)}
              title={`${s.account}: ${STATE[s.state]?.[0] || s.state}`}>
              {new Date(`${s.day}T12:00:00`).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })}
              <span aria-hidden style={{ display: "inline-block", width: 7, height: 7, borderRadius: 4, marginLeft: 7, background: STATE[s.state]?.[1] }} />
            </button>))}
        </div>)}
      {cur && (
        <section className="panel" aria-labelledby="rv-h" data-testid="review-summary" style={{ marginBottom: 14 }}>
          <div className="ph">
            <h2 id="rv-h">Session {cur.day}</h2>
            <span className="hint" style={{ color: STATE[cur.state]?.[1] }} data-testid="review-state">{STATE[cur.state]?.[0] || cur.state}</span>
            <span style={{ flex: 1 }} />
            {["none", "interrupted"].includes(cur.state) && <button type="button" className="btn" onClick={() => runNow(false)}>Run this review</button>}
            {["done", "partial"].includes(cur.state) && <button type="button" className="btn ghost" onClick={() => runNow(true)}>Run again</button>}
          </div>
          <div className="pb">
            {msg && <p className="ink2" role="status">{msg}</p>}
            {one.error && <Err e={one.error} />}
            {cur.state === "queued" && <p className="ink2">Queued. Reviews run one at a time and never while the market is open, so the live bot keeps the computer's attention.</p>}
            {cur.state === "running" && <p className="ink2">Running now (usually several minutes). Steps finished so far are listed below.</p>}
            {rev ? (
              <ul className="findings" data-testid="review-headline" style={{ margin: 0, paddingLeft: 20, lineHeight: 1.6 }}>
                {rev.headline.map((h: string, k: number) => <li key={k} style={{ marginBottom: 4 }}>{h}</li>)}
              </ul>) : (cur.state === "none" && <p className="ink2">Not run yet for this session. Use “Run this review”; it starts after the close if a session is on.</p>)}
            {rev && <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>{rev.caution} Paper account {cur.account}.</p>}
            {st?.steps && (
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 12, fontSize: 13 }} data-testid="review-steps">
                {Object.entries(st.steps).map(([k, v]: [string, any]) => (
                  <span key={k} title={v.error || ""} style={{ color: v.state === "failed" ? "var(--crit)" : v.state === "done" ? "var(--ink2)" : "var(--muted)" }}>
                    {v.state === "done" ? "✓" : v.state === "failed" ? "✗" : "…"} {STEP[k] || k}{v.seconds != null ? ` · ${Math.round(v.seconds)} s` : ""}
                    {v.state === "failed" && <> — {v.error}</>}
                  </span>))}
              </div>)}
          </div>
        </section>)}
      {rev?.trade_report && <Trades tr={rev.trade_report} />}
      {rev?.loss_study && <Loss ls={rev.loss_study} />}
      {rev?.day_backtest && <Backtest bt={rev.day_backtest} />}
      {rev?.monte_carlo && <MonteCarlo mc={rev.monte_carlo} />}
    </>
  );
}

function Loss({ ls }: { ls: any }) {
  const [f, setF] = useState(0);
  const fac = ls.factors?.[f];
  return (
    <Panel id="rv-loss" title="Why trades lost" hint={`${ls.trades} round trips · win rate ${pct(ls.win_rate, 0)} · ${signed(ls.mean_inr)} ₹ per trade`}>
      <p className="ink2" style={{ marginTop: 0 }}>A group is called out only when it has at least 15 trades and loses more than the day’s average beyond chance.
        {ls.reconciliation && <> Reconciled with the paper ledger: {ls.reconciliation.matched} of {ls.reconciliation.ledger_round_trips} round trips by order, quantity, price and net P&amp;L{ls.journal_coverage != null && <>; market context recorded for {pct(ls.journal_coverage, 0)} of them</>}.</>}</p>
      {ls.factors?.length > 0 && (
        <div className="seg" role="group" aria-label="Factor" style={{ marginBottom: 10, flexWrap: "wrap" }}>
          {ls.factors.map((x: any, k: number) => <button key={x.factor} type="button" aria-pressed={k === f} onClick={() => setF(k)}>{x.label}</button>)}
        </div>)}
      {fac && <DataTable rows={fac.buckets} maxHeight={360} cols={[
        { key: "bucket", label: fac.label, f: (b: any) => <>{b.finding && <b className="down" title="loses more than the rest, beyond chance">● </b>}{b.bucket}</> },
        { key: "trades", label: "Trades", r: true },
        { key: "win_rate", label: "Win rate", r: true, f: (b: any) => pct(b.win_rate, 0) },
        { key: "net_inr", label: "Net ₹", r: true, f: (b: any) => <span className={b.net_inr < 0 ? "down" : "up"}>{inr(b.net_inr)}</span> },
        { key: "mean_inr", label: "₹ per trade", r: true, f: (b: any) => signed(b.mean_inr) },
        { key: "ci", label: "95% range of ₹ per trade", r: true, f: (b: any) => `${signed(b.ci95?.[0])} to ${signed(b.ci95?.[1])}` },
      ]} />}
    </Panel>
  );
}

function Backtest({ bt }: { bt: any }) {
  const [by, setBy] = useState<"by_family" | "by_timeframe" | "by_stop">("by_family");
  const lab: Record<string, string> = { by_family: "Strategy type", by_timeframe: "Chart speed", by_stop: "Stop size" };
  return (
    <Panel id="rv-bt" title="Every strategy on its own" hint={`${num(bt.strategies, 0)} strategies · each with its own ₹1,00,000`}>
      <p className="ink2" style={{ marginTop: 0 }}>{num(bt.traded, 0)} strategies traded; {pct(bt.share_positive, 0)} of them made money after charges (about 50% would by luck alone).
        The challenger on the same day: {bt.challenger?.trades} trades, {inr(bt.challenger?.net_inr)}. Judge types across their many variants, not the single best.</p>
      <div className="seg" role="group" aria-label="Group by" style={{ marginBottom: 10 }}>
        {(Object.keys(lab) as (keyof typeof lab)[]).map((k) => <button key={k} type="button" aria-pressed={by === k} onClick={() => setBy(k as any)}>{lab[k]}</button>)}
      </div>
      <DataTable rows={bt[by]} maxHeight={360} cols={[
        { key: "group", label: lab[by] },
        { key: "strategies", label: "Strategies", r: true, f: (g: any) => num(g.strategies, 0) },
        { key: "traded", label: "Traded", r: true, f: (g: any) => num(g.traded, 0) },
        { key: "trades", label: "Trades", r: true, f: (g: any) => num(g.trades, 0) },
        { key: "net_per_trade", label: "₹ per trade", r: true, f: (g: any) => <span className={(g.net_per_trade ?? 0) < 0 ? "down" : "up"}>{signed(g.net_per_trade)}</span> },
        { key: "share_positive", label: "Made money", r: true, f: (g: any) => pct(g.share_positive, 0) },
        { key: "charges_inr", label: "Charges ₹", r: true, f: (g: any) => inr(g.charges_inr) },
      ]} />
    </Panel>
  );
}

function MonteCarlo({ mc }: { mc: any }) {
  return (
    <Panel id="rv-mc" title="Trade-order luck" hint={mc.shuffles ? `${num(mc.shuffles, 0)} shuffles · seed ${mc.seed}` : undefined}>
      {mc.status ? <p className="ink2" style={{ margin: 0 }}>{mc.status} ({mc.trades} closed trades).</p> : (
        <>
          <p className="ink2" style={{ marginTop: 0 }}>The same {mc.trades} trades in a different order give the same total ({inr(mc.net_pnl_inr)}) but a different worst dip.</p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
            {[["What happened", mc.actual_max_drawdown], ["Typical order", mc.median], ["1 in 20 orders", mc.p95_worst], ["Worst order seen", mc.worst]].map(([k, v]) => (
              <div key={k as string} className="metric sm"><div className="k">{k}</div><div className="v down">{pct(Math.abs(v as number), 2)}</div></div>))}
          </div>
          {mc.sequence_risk && <p className="down" style={{ marginBottom: 0 }}>The 1-in-20 dip is at least double what happened: expect a dip like that live eventually.</p>}
        </>)}
    </Panel>
  );
}

function Trades({ tr }: { tr: any }) {
  return (
    <>
      {tr.next_time?.length > 0 && (
        <Panel id="rv-next" title="Next time" hint="each change goes live only after it beats the current rules on other days">
          <ol data-testid="review-next" style={{ margin: 0, paddingLeft: 20, lineHeight: 1.55 }}>
            {tr.next_time.map((x: any) => (
              <li key={x.pattern} style={{ marginBottom: 8 }}>
                <b>{x.next_time}</b>
                <div className="ink2" style={{ fontSize: 13.5 }}>{x.meaning} {x.trades} trade(s), {inr(x.lost_inr)} lost. Decided by: {x.decided_by}.</div>
              </li>))}
          </ol>
        </Panel>)}
      <Panel id="rv-trades" title="Every trade today" hint={tr.plain}>
        <DataTable rows={tr.trades} maxHeight={420} wrap empty="No completed trades." cols={[
          { key: "symbol", label: "Stock" },
          { key: "bought", label: "Bought", f: (t: any) => `${t.bought ?? "—"} @ ${num(t.buy_price, 2)}` },
          { key: "sold", label: "Sold", f: (t: any) => `${t.sold} @ ${num(t.sell_price, 2)}` },
          { key: "net_inr", label: "Result ₹", r: true, f: (t: any) => <span className={t.net_inr < 0 ? "down" : "up"}>{inr(t.net_inr, 2)}</span> },
          { key: "why", label: "Why", f: (t: any) => <span title={(t.also || []).join("; ")}>{t.why}</span> },
          { key: "strategy", label: "Strategy" },
        ]} />
      </Panel>
    </>
  );
}
