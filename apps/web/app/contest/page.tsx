"use client";
/* Incumbent vs challenger: equity curves, drawdowns, net P&L, costs and the current verdict, from /api/contest.
   Forward paper, historical replay and synthetic results are labelled separately; only forward results can decide,
   and promotion is the operator's decision. Paper (simulated) orders only. */
import { useMemo, useState } from "react";
import { LineChart } from "@/components/charts";
import { Err, Skeleton } from "@/components/ui";
import { inr, useApi } from "@/lib/api";

const pct = (x: number | null | undefined, d = 2) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const signed = (x: number) => `${x >= 0 ? "+" : "−"}${inr(Math.abs(x))}`;
const VERDICT: Record<string, string> = {
  "challenger ahead": "var(--good)", "incumbent ahead": "var(--crit)", inconclusive: "var(--warn)", "not a forward verdict": "var(--muted)",
};

function drawdown(eq: number[], start: number) {
  let peak = start;
  return eq.map((v) => { peak = Math.max(peak, v); return peak ? (v / peak - 1) * 100 : 0; });
}

export default function Contest() {
  const { data, error } = useApi<any>("/contest", 60000);
  const sources = useMemo(() => {
    const out: [string, string, any][] = [];
    if (data?.forward && !data.forward.error) out.push(["forward", "Forward paper", data.forward]);
    (data?.replays || []).forEach((r: any, k: number) => out.push([`r${k}`, `${r.kind === "historical" ? "Historical replay" : "Synthetic replay"} · ${r.folder}`, r]));
    return out;
  }, [data]);
  const [pick, setPick] = useState<string>("");
  const cur = sources.find((s) => s[0] === pick) || sources[0];
  return (
    <>
      <h1>Incumbent vs challenger</h1>
      <p className="sub">Two isolated paper accounts receive the same prices at the same moments. Only forward paper sessions can produce a
        verdict; promotion is always your decision. Real-money orders are switched off.</p>
      {error && <Err e={error} />}
      {!data && !error && <Skeleton lines={6} />}
      {data && !sources.length && (
        <section className="panel" data-testid="contest-empty">
          <h2>No contest yet</h2>
          <p className="ink2">Run one from a clean checkout: <code>python -m nseql.cli contest --fixture</code> (synthetic data), or start live
            screening: the challenger then runs beside the live bot and the forward contest appears here.</p>
          {data.forward?.error && <p className="down">Forward contest: {data.forward.error}</p>}
        </section>)}
      {sources.length > 1 && (
        <div className="seg" role="group" aria-label="Contest source" style={{ marginBottom: 12, flexWrap: "wrap" }}>
          {sources.map(([k, l]) => <button key={k} type="button" aria-pressed={cur?.[0] === k} onClick={() => setPick(k)}>{l}</button>)}
        </div>)}
      {cur && <Report r={cur[2]} />}
    </>
  );
}

function Report({ r }: { r: any }) {
  const inc = r.incumbent, ch = r.challenger, v = r.verdict, p = r.primary;
  const days: string[] = r.sessions || [];
  const eqI = inc.daily.map((d: any) => d.equity), eqC = ch.daily.map((d: any) => d.equity);
  const cap = r.capital;
  const rows: [string, (a: any) => string][] = [
    ["Net P&L after costs", (a) => (a ? signed(a.net_pnl_inr) : inr(0))],
    ["Maximum drawdown", (a) => pct(a ? a.max_drawdown : 0)],
    ["Worst day", (a) => pct(a ? a.worst_day : 0)],
    ["Closed trades", (a) => (a ? String(a.trades_closed) : "0")],
    ["Charges paid", (a) => inr(a ? a.charges_inr : 0)],
    ["Turnover (× capital)", (a) => (a ? a.turnover.toFixed(2) : "0")],
    ["Average exposure", (a) => pct(a ? a.exposure_avg : 0, 1)],
    ["Partial / cancelled orders", (a) => (a ? `${a.orders_partial} / ${a.orders_cancelled_or_rejected}` : "—")],
    ["Buys refused by limits", (a) => (a ? a.buys_refused_by_limits.toLocaleString("en-IN") : "—")],
    ["Residual positions (not closed)", (a) => (a ? String(a.residual_positions.length) : "—")],
  ];
  return (
    <>
      <section className="panel" style={{ marginBottom: 14, borderLeft: `4px solid ${VERDICT[v.status] || "var(--muted)"}` }} aria-labelledby="verdict-h"
        data-testid="contest-verdict">
        <h2 id="verdict-h" style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
          Verdict: <span style={{ color: VERDICT[v.status] }}>{v.status}</span></h2>
        <p className="ink2" style={{ margin: "4px 0 10px" }}>{v.reason}</p>
        <dl className="kv" style={{ display: "grid", gridTemplateColumns: "minmax(150px,auto) 1fr", gap: "4px 14px", margin: 0 }}>
          <dt className="dim">Data source</dt><dd data-testid="contest-source"><b>{r.kind_label}</b></dd>
          <dt className="dim">Sessions</dt><dd>{days.length ? `${days[0]} → ${days[days.length - 1]} (${days.length})` : "none yet"}</dd>
          <dt className="dim">Incumbent</dt><dd>{inc.version}</dd>
          <dt className="dim">Challenger</dt><dd>{ch.version}</dd>
          <dt className="dim">Starting capital</dt><dd>{inr(cap)} each</dd>
          <dt className="dim">Primary measure</dt><dd>Paired daily return difference (challenger − incumbent): mean {pct(p.mean, 3)},
            95% interval [{pct(p.low, 3)}, {pct(p.high, 3)}]</dd>
          <dt className="dim">Rules</dt><dd>{v.rules.min_sessions} sessions and {v.rules.min_challenger_trades} challenger trades minimum; interval
            above zero, positive net P&L, drawdown no worse</dd>
          {r.manifest_sha256 && <><dt className="dim">Frozen manifest</dt><dd className="num" style={{ wordBreak: "break-all" }}>{r.manifest_sha256}</dd></>}
        </dl>
        {r.both_lost_money && <p className="down" style={{ margin: "10px 0 0" }} data-testid="both-lost">Both traders lost money: cash did better than either.</p>}
      </section>
      <div className="grid g2" style={{ marginBottom: 14 }}>
        <section className="panel" aria-labelledby="eq-h" data-testid="contest-equity">
          <h2 id="eq-h">Account value by session</h2>
          {days.length ? <LineChart x={days} height={220} fmt={(x) => inr(x)}
            series={[{ name: "incumbent", values: eqI }, { name: "challenger", values: eqC }, { name: "cash", values: days.map(() => cap) }]} />
            : <p className="dim">No completed sessions yet.</p>}
        </section>
        <section className="panel" aria-labelledby="dd-h" data-testid="contest-drawdown">
          <h2 id="dd-h">Drawdown from peak</h2>
          {days.length ? <LineChart x={days} height={220} zero fmt={(x) => `${x.toFixed(2)}%`}
            series={[{ name: "incumbent", values: drawdown(eqI, cap) }, { name: "challenger", values: drawdown(eqC, cap) }]} />
            : <p className="dim">No completed sessions yet.</p>}
        </section>
      </div>
      <section className="panel" aria-labelledby="m-h" style={{ marginBottom: 14 }}>
        <h2 id="m-h">Results after costs</h2>
        <div style={{ overflowX: "auto" }}>
          <table className="tbl" data-testid="contest-table">
            <thead><tr><th scope="col">Measure</th><th scope="col">Incumbent</th><th scope="col">Challenger</th><th scope="col">Cash</th></tr></thead>
            <tbody>{rows.map(([label, f]) => (
              <tr key={label}><th scope="row" style={{ fontWeight: 500 }}>{label}</th><td className="num">{f(inc)}</td><td className="num">{f(ch)}</td>
                <td className="num dim">{label.startsWith("Net") || label.startsWith("Charges") ? inr(0) : label.startsWith("Max") || label.startsWith("Worst") || label.startsWith("Average") ? "0.00%" : "—"}</td></tr>))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
