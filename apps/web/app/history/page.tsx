"use client";
/* Historical Validation (services/screening/history_validation.py): months of real NSE 1-minute history replayed through
   the exact live rules and the challenger, with a registered holdout that only grows forward, benchmarks, placebo and
   stress, plus the concept and feature studies on the same data. Runs on the laptop, weekly, outside market hours. */
import { useMemo, useState } from "react";
import { LineChart } from "@/components/charts";
import { Tag, signedPct } from "@/components/vega";
import { post, useApi } from "@/lib/api";

const SEG = ["development", "validation", "holdout", "contaminated"] as const;
const SEG_NOTE: Record<string, string> = {
  development: "first half of the history before the live account", validation: "next quarter", holdout: "registered once; only grows forward",
  contaminated: "sessions the live rules were shaped on: shown, never counted as evidence" };
const WHO = ["incumbent", "B uncertainty-aware", "B0 matched control", "challenger", "NIFTY 50", "buy and hold watchlist", "cash"];
const tone = (r?: string) => (r?.startsWith("ahead") ? "ok" : r?.startsWith("behind") ? "no" : "wait");

export default function History() {
  const q = useApi<any>("/history", 60000);
  const [msg, setMsg] = useState<string | null>(null);
  const [seg, setSeg] = useState<string>("holdout");
  const st = q.data?.status, r = q.data?.report;
  const curves = useMemo(() => {
    if (!r?.curves?.days?.length) return null;
    const cum = (a: (number | null)[]) => { let v = 1; return a.map((x) => (v *= 1 + (x ?? 0)) - 1); };
    return { x: r.curves.days, series: WHO.filter((k) => r.curves[k]).map((k) => ({ name: k, values: cum(r.curves[k]) })) };
  }, [r]);
  const s = r?.segments?.[seg];
  const run = async () => { setMsg(null); try { const x = await post("/history/run"); setMsg(`Result: ${x.result}.`); q.reload(); } catch (e: any) { setMsg(`Not started: ${e.message}`); } };
  return (
    <>
      <div className="vhead"><div><h1>Historical Validation</h1><p>The live rules and the challenger replayed minute by minute on months of real NSE history they were not designed on.</p></div>
        <div className="row" style={{ alignItems: "center" }}>
          {st && <Tag tone={st.state === "done" ? "ok" : st.state === "failed" ? "no" : st.state === "never run" ? "" : "wait"} testid="hv-state">{st.state}</Tag>}
          <button onClick={run} data-testid="hv-run" title="Operator key only; refused during market hours">Run now</button></div></div>
      {msg && <div className="banner info" role="status">{msg}</div>}
      {st && st.state !== "done" && st.state !== "never run" && <div className="banner info" data-testid="hv-progress">
        <b>{st.phase}</b>{st.sessions ? ` · ${st.done ?? 0} of ${st.sessions} sessions replayed` : ""}{st.detail ? ` · ${st.detail}` : ""}{st.error ? ` · ${st.error}` : ""}</div>}
      {!r && <section className="vp"><div className="b"><p className="empty" style={{ textAlign: "left" }}>
        No historical validation yet. It runs on the laptop once a week after the close (or Run now): it imports 12 months of Angel One
        1-minute candles for the watchlist and NIFTY 50, then replays the live rules and the challenger on them. Expect a few hours; it pauses during market hours.</p></div></section>}
      {r && <>
        <div className="banner" style={{ borderLeftColor: r.kind.startsWith("SYNTHETIC") ? "var(--warn)" : "var(--good)" }} data-testid="hv-kind">
          <b>{r.kind}</b> · run {r.run} · {r.incumbent_rules} vs {r.challenger} · capital ₹{Number(r.capital).toLocaleString("en-IN")} each</div>
        <div className="vseg" role="group" aria-label="Segment" style={{ marginBottom: 12 }}>
          {SEG.map((k) => <button key={k} aria-pressed={seg === k} onClick={() => setSeg(k)}>{k} {r.segments[k]?.sessions?.length ? `(${r.segments[k].sessions.length})` : "(0)"}</button>)}
        </div>
        <section className="vp" aria-labelledby="seg-h">
          <div className="h"><div><h2 id="seg-h">{seg[0].toUpperCase() + seg.slice(1)}</h2><div className="sub2">{SEG_NOTE[seg]}{s?.sessions?.length ? ` · ${s.sessions[0]} .. ${s.sessions[s.sessions.length - 1]}` : ""}</div></div></div>
          {s?.sessions?.length ? <>
            <div className="tw"><table data-testid="hv-segment"><thead><tr><th>Series</th><th className="r">Total after costs</th><th className="r">Mean per day [95%]</th><th className="r">Max drawdown</th><th className="r">Sharpe</th><th className="r">Worst day</th></tr></thead>
              <tbody>{WHO.filter((k) => s[k]).map((k) => <tr key={k}><td><b>{k}</b></td><td className="r"><span className={s[k].total_return >= 0 ? "up" : "down"}>{signedPct(s[k].total_return)}</span></td>
                <td className="r">{signedPct(s[k].mean_daily, 3)} <span className="dim">[{signedPct(s[k].ci_low, 3)}, {signedPct(s[k].ci_high, 3)}]</span></td>
                <td className="r">{signedPct(s[k].max_drawdown)}</td><td className="r">{s[k].sharpe == null ? "—" : s[k].sharpe.toFixed(2)}</td><td className="r">{signedPct(s[k].worst_day)}</td></tr>)}</tbody></table></div>
            <div className="b" style={{ paddingTop: 12 }}>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }} data-testid="hv-readings">{["incumbent", "challenger"].flatMap((w) => Object.entries(s.vs?.[w] || {}).map(([b, v]: any) =>
                <Tag key={`${w}-${b}`} tone={tone(v.reading)}>{w} vs {b}: {v.reading || "too few sessions"}</Tag>))}
                {s.challenger_vs_incumbent?.reading && <Tag tone={tone(s.challenger_vs_incumbent.reading)}>challenger vs incumbent: {s.challenger_vs_incumbent.reading}</Tag>}</div>
              <ul className="list" style={{ marginTop: 8 }}>{(s.plain || []).map((p: string) => <li key={p} className="t" style={{ fontSize: 13 }}>{p}</li>)}</ul>
            </div></> : <p className="empty">No sessions in this segment.</p>}
        </section>
        {s?.ablation && <section className="vp" style={{ marginTop: 14 }} aria-labelledby="ab-h">
          <div className="h"><div><h2 id="ab-h">Is the new brain better? ({seg})</h2><div className="sub2">Paired by session, block-bootstrap interval (shown from 20 sessions); B0 keeps A's ranking but matches B's trade count and ticket size, so B vs B0 separates better selection from merely trading less or smaller</div></div></div>
          <div className="tw"><table data-testid="hv-ablation"><thead><tr><th>Comparison</th><th className="r">Mean daily difference</th><th className="r">95% interval</th><th>Reading</th></tr></thead>
            <tbody>{Object.entries(s.ablation).map(([k, v]: any) => <tr key={k}><td>{k}</td><td className="r">{signedPct(v.mean_daily_excess, 3)}</td>
              <td className="r">{v.ci_low == null || String(v.reading || "").startsWith("too few") ? "—" : `${signedPct(v.ci_low, 3)} .. ${signedPct(v.ci_high, 3)}`}</td><td><Tag tone={tone(v.reading)}>{v.reading || "too few sessions"}</Tag></td></tr>)}</tbody></table></div>
          {r.variants && <div className="tw" style={{ marginTop: 8 }}><table data-testid="hv-quality"><thead><tr><th>Variant (all counted sessions)</th><th className="r">Round trips</th><th className="r">Mean per trade</th><th className="r">Share won</th><th className="r">Charges</th><th className="r">Net</th></tr></thead>
            <tbody>{Object.entries(r.variants.trade_quality).map(([k, v]: any) => <tr key={k}><td>{k}</td><td className="r">{v.round_trips}</td><td className="r">{v.mean_net_inr == null ? "—" : `₹${v.mean_net_inr}`}</td>
              <td className="r">{v.share_won == null ? "—" : `${Math.round(v.share_won * 100)}%`}</td><td className="r">{v.charges_inr == null ? "—" : `₹${Math.round(v.charges_inr).toLocaleString("en-IN")}`}</td>
              <td className="r">{v.net_inr == null ? "—" : `₹${Math.round(v.net_inr).toLocaleString("en-IN")}`}</td></tr>)}</tbody></table></div>}
          {r.variants?.warnings?.length > 0 && <div className="banner info" role="note" data-testid="hv-variant-warnings" style={{ margin: "8px 0 0" }}>{r.variants.warnings.join(" · ")}</div>}
          {r.variants?.C_market_context && <div className="ft"><span>C (market context) and D (combined): {r.variants.C_market_context.status}: {r.variants.C_market_context.why}</span></div>}
        </section>}
        {curves && <section className="vp" style={{ marginTop: 14 }} aria-labelledby="cv-h">
          <div className="h"><div><h2 id="cv-h">Cumulative result, all counted sessions</h2><div className="sub2">After costs; holdout from {r.protocol.holdout_from}; contaminated from {r.protocol.contaminated_from}</div></div></div>
          <div className="b"><LineChart x={curves.x} series={curves.series} height={240} fmt={(v) => signedPct(v, 1)} zero /></div></section>}
        <div className="grid g3" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="tr-h"><div className="h"><h2 id="tr-h">Holdout trades</h2></div>
            <div className="b vlist" data-testid="hv-trades">
              <div><span>Round trips (holdout / all)</span><b>{r.trades.round_trips_holdout} / {r.trades.round_trips_all}</b></div>
              <div><span>Beats random entries? (placebo p)</span><b>{r.trades.placebo_holdout?.p ?? "—"}</b></div>
              <div><span>Costs doubled</span><b>{r.trades.stress_holdout?.double_costs_inr == null ? "—" : `₹${Math.round(r.trades.stress_holdout.double_costs_inr).toLocaleString("en-IN")}`}</b></div>
              <div><span>One minute later</span><b>{r.trades.stress_holdout?.one_minute_delay_inr == null ? "—" : `₹${Math.round(r.trades.stress_holdout.one_minute_delay_inr).toLocaleString("en-IN")}`}</b></div>
            </div></section>
          <section className="vp" aria-labelledby="cs-h"><div className="h"><h2 id="cs-h">Concepts and features on this data</h2></div>
            <div className="b vlist">
              <div><span>Holdout-supported concepts</span><b>{r.concepts ? (r.concepts.candidates.length ? r.concepts.candidates.join(", ") : "none") : "—"}</b></div>
              {r.features && Object.entries(r.features.verdicts).map(([f, v]: any) => <div key={f}><span>{f}</span><b>{v}</b></div>)}
            </div><div className="ft"><span>Full tables: Brain Intelligence and Heatmap Intelligence.</span></div></section>
          <section className="vp" aria-labelledby="cov-h"><div className="h"><h2 id="cov-h">Data coverage</h2></div>
            <div className="tw" style={{ ["--mh" as any]: "260px" }} tabIndex={0} aria-label="Data coverage (scrollable)"><table data-testid="hv-coverage"><thead><tr><th>Symbol</th><th className="r">Sessions</th><th>From</th><th>To</th></tr></thead>
              <tbody>{r.dataset.symbols.map((c: any) => <tr key={c.symbol}><td>{c.symbol}</td><td className="r">{c.sessions}</td><td>{c.first}</td><td>{c.last}</td></tr>)}</tbody></table></div></section>
        </div>
        <section className="vp" style={{ marginTop: 14 }} aria-labelledby="lim-h"><div className="h"><h2 id="lim-h">Limits of this evidence</h2></div>
          <div className="b"><ul className="list">{r.limits.map((x: string) => <li key={x} className="t" style={{ fontSize: 13 }}>{x}</li>)}</ul>
            <p className="dim" style={{ fontSize: 12, margin: "8px 0 0" }}>Protocol registered {String(r.protocol.registered_at).replace("T", " ")}: {r.protocol.rule}. Results are recorded in the evidence ledger (HIST-incumbent, HIST-challenger); a historical result never promotes a rule by itself.</p></div></section>
      </>}
    </>
  );
}
