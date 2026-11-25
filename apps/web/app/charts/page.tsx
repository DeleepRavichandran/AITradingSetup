"use client";
/* Chart Analyst (services/screening/chart_analysis.py) from /api/screening/chart: 5-minute candles with EMA20, EMA50 and
   VWAP, support and resistance levels, the account's own buys and sells, RSI(14), trend on 5m/15m/daily, patterns and a plain
   read. "As of" replays the chart as it stood at a time of day (completed candles before it only).
   Market structure (services/screening/market_structure.py) from /api/structure: BOS/CHoCH/MSS, fair value gaps, order /
   breaker / mitigation blocks, liquidity levels and sweeps, value area; each concept with its research verdict
   (concept_study.py). Patterns are inferred from candles; order flow is not available and is never drawn. */
import { useState } from "react";
import { Candles, LineChart, type Note, type Zone } from "@/components/charts";
import { Tag } from "@/components/vega";
import { Err, Panel, Skeleton } from "@/components/ui";
import { num, pct, useApi } from "@/lib/api";

const TREND: Record<string, string> = { up: "var(--good)", down: "var(--crit)", mixed: "var(--warn)" };

export default function Charts() {
  const [sym, setSym] = useState("");
  const [at, setAt] = useState("");
  const first = useApi<any>(sym ? null : "/screening/chart?symbol=RELIANCE", 0);
  const q = sym ? `/screening/chart?symbol=${encodeURIComponent(sym)}${at ? `&at=${at}` : ""}` : null;
  const cur = useApi<any>(q, at ? 0 : 60000);
  const d = cur.data || first.data;
  const err = cur.error || (!sym && first.error);
  const s = d?.series;
  const markers = (d?.trades || []).map((t: any) => {
    const i = (s?.t || []).findIndex((x: string) => x.slice(11, 16) >= t.t);
    return i < 0 ? null : { ts: s.t[i], price: t.price, side: t.side === "BUY" ? 1 : -1, label: t.side === "BUY" ? "buy" : `sell ${t.profit != null ? (t.profit >= 0 ? "+" : "") + num(t.profit, 0) : ""}` };
  }).filter(Boolean);
  const near = [d?.support, d?.resistance].filter(Boolean).map((l: any) => ({ price: l.price, label: `${l.label} ${num(l.price, 2)}` }));
  const symNow = sym || d?.symbol;
  const ms = useApi<any>(symNow ? `/structure?symbol=${encodeURIComponent(symNow)}${at ? `&at=${at}` : ""}` : null, at ? 0 : 60000).data;
  const [layers, setLayers] = useState<Record<string, boolean>>({ structure: true, gaps: true, blocks: true, liquidity: true, value: false });
  const BULL = "var(--good)", BEAR = "var(--crit)", BLOCK = "var(--data)", LIQ = "var(--warn)", VAL = "var(--violet)";
  const zones: Zone[] = (ms?.zones || []).filter((z: any) => !z.invalidated_at || at).flatMap((z: any) => {
    const gap = z.kind.startsWith("fvg") || z.kind.startsWith("bpr"), block = /^(ob|breaker|mitigation)/.test(z.kind);
    if ((gap && !layers.gaps) || (block && !layers.blocks)) return [];
    return [{ from: z.observable_at, to: z.invalidated_at, lo: z.lo, hi: z.hi, dashed: !!z.mitigated_at,
      color: block ? BLOCK : z.direction > 0 ? BULL : BEAR, label: `${z.kind.replace(/_/g, " ")} ${z.lo}–${z.hi} (observable ${String(z.observable_at).slice(11, 16)})` }];
  });
  const SHORT: Record<string, string> = { bos_up: "BOS↑", bos_down: "BOS↓", choch_up: "CHoCH↑", choch_down: "CHoCH↓", mss_up: "MSS↑", mss_down: "MSS↓",
    sweep_bsl: "BSL swept", sweep_ssl: "SSL swept", false_breakout_up: "false BO", false_breakout_down: "false BO", spring: "spring", upthrust: "upthrust" };
  const notes: Note[] = (ms?.events || []).filter((e: any) => SHORT[e.type] && ((layers.structure && /bos|choch|mss/.test(e.type)) || (layers.liquidity && !/bos|choch|mss/.test(e.type))))
    .map((e: any) => ({ ts: e.observable_at, price: e.price, label: SHORT[e.type], color: e.direction > 0 ? BULL : BEAR, up: e.direction > 0 }));
  const lvl = layers.liquidity ? (ms?.levels || []).filter((l: any) => l.active && !/swing/.test(l.kind) && !/value|point of control/.test(l.kind))
    .map((l: any) => ({ price: l.price, label: `${l.kind} ${num(l.price, 2)}`, color: LIQ })) : [];
  const val = layers.value && ms?.profile_today ? [{ price: ms.profile_today.vah, label: `VAH ${num(ms.profile_today.vah, 2)}`, color: VAL },
    { price: ms.profile_today.poc, label: `POC ${num(ms.profile_today.poc, 2)}`, color: VAL }, { price: ms.profile_today.val, label: `VAL ${num(ms.profile_today.val, 2)}`, color: VAL }] : [];
  const verdictTone = (v?: string) => (v === "candidate" ? "ok" : v === "insufficient sample" ? "wait" : v ? "no" : "");
  return (
    <>
      <h1>Chart Analyst</h1>
      <p className="sub">How a trader would read each stock's chart right now: trend on three timeframes, support and resistance, indicators and
        patterns, with the bot's own buys and sells marked. Built only from completed candles, so it never sees the future.</p>
      <div className="row" style={{ gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
        <label>Stock
          <select id="chart-symbol" value={sym || d?.symbol || ""} onChange={(e) => setSym(e.target.value)}>
            {(d?.watchlist || []).map((x: string) => <option key={x} value={x}>{x}</option>)}
          </select></label>
        <label>Chart as of (optional)
          <input id="chart-at" type="time" min="09:15" max="15:30" value={at} onChange={(e) => { setAt(e.target.value); if (!sym && d) setSym(d.symbol); }} /></label>
        {at && <button type="button" className="ghost" onClick={() => setAt("")}>Back to live</button>}
      </div>
      {err && <Err e={err} />}
      {!d && !err && <Skeleton lines={8} />}
      {d && (
        <div className="grid g2" style={{ alignItems: "start" }}>
          <div style={{ minWidth: 0 }}>
            <Panel id="ch-candles" title={`${d.symbol} · 5-minute`} hint={d.price ? `last ${num(d.price, 2)} · as of ${String(d.as_of).replace("T", " ")}` : undefined}>
              <div className="row" role="group" aria-label="Structure layers" style={{ gap: 12, marginBottom: 6, fontSize: 12.5 }}>
                {[["structure", "BOS / CHoCH / MSS"], ["gaps", "Fair value gaps"], ["blocks", "Order / breaker blocks"], ["liquidity", "Liquidity and sweeps"], ["value", "Value area"]].map(([k, l]) =>
                  <label key={k} style={{ display: "inline-flex", gap: 5, alignItems: "center", fontSize: 12.5 }}><input type="checkbox" checked={!!layers[k]}
                    onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })} />{l}</label>)}
              </div>
              {s?.t?.length ? <Candles ts={s.t} open={s.open} high={s.high} low={s.low} close={s.close} markers={markers as any} lines={[...near, ...lvl, ...val]} height={340}
                zones={zones} notes={notes}
                overlays={[{ label: "EMA 20", values: s.ema20, color: "var(--s1)" }, { label: "EMA 50", values: s.ema50, color: "var(--s3)" },
                  { label: "VWAP", values: s.vwap, color: "var(--s5)" }]} /> : <p className="dim">No completed 5-minute candles yet.</p>}
            </Panel>
            {s?.rsi14?.some((v: any) => v != null) && (
              <div style={{ marginTop: 12 }}><Panel id="ch-rsi" title="RSI (14)" hint="above 70 overbought · below 30 oversold">
                <LineChart x={s.t.map((t: string) => t.slice(11, 16))} series={[{ name: "RSI", values: s.rsi14 }, { name: "70", values: s.t.map(() => 70) },
                  { name: "30", values: s.t.map(() => 30) }]} height={140} fmt={(v) => num(v, 0)} />
              </Panel></div>)}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
            <Panel id="ch-ms" title="Market structure" hint={ms ? `${ms.timeframe} · trend ${ms.trend || "none"} · 15m ${ms.htf_trend_15m || "none"}` : "loading"}>
              <p className="dim" style={{ margin: "0 0 8px", fontSize: 12.5 }} data-testid="ms-labels">
                <b className="ink2">Observed:</b> {ms?.labels?.observed}. <b className="ink2">Inferred:</b> {ms?.labels?.inferred}. <b className="ink2">Validated:</b> {ms?.labels?.validated}.</p>
              {ms?.events?.length ? <div className="tw" style={{ ["--mh" as any]: "260px" }} tabIndex={0} aria-label="Structure events today (scrollable)"><table data-testid="ms-events">
                <thead><tr><th>Seen</th><th>Pattern</th><th>Detail</th><th>Research verdict</th></tr></thead>
                <tbody>{[...ms.events].reverse().slice(0, 40).map((e: any, i: number) => { const v = ms.verdicts?.[e.type]?.[0]; return (
                  <tr key={i}><td>{String(e.observable_at).slice(11, 16)}</td><td>{e.type.replace(/_/g, " ")}<span className="st">{e.family}{e.session ? ` · ${e.session.split(" ")[0]}` : ""}</span></td>
                    <td style={{ whiteSpace: "normal", minWidth: 160, fontSize: 12.5 }}>{e.detail}{e.formed_at !== e.observable_at && <span className="st">formed {String(e.formed_at).slice(11, 16)}</span>}</td>
                    <td>{v ? <Tag tone={verdictTone(v.verdict)}>{v.verdict}</Tag> : <span className="dim">not studied yet</span>}{v && <span className="st">n {v.n}</span>}</td></tr>); })}</tbody>
              </table></div> : <p className="dim" style={{ margin: 0 }}>No structure event in this session yet.</p>}
              <p className="dim" style={{ fontSize: 12, margin: "8px 0 0" }} data-testid="ms-unavailable">Not available from 1-minute candles, so never drawn: {(ms?.unavailable || []).join(", ")}.</p>
            </Panel>
            <Panel id="ch-read" title="The read">
              <ul data-testid="chart-read" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>{(d.read || []).map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
            </Panel>
            <Panel id="ch-trend" title="Trend">
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }} data-testid="chart-trend">
                {Object.entries(d.trend || {}).map(([k, v]: [string, any]) => (
                  <span key={k} style={{ padding: "4px 10px", borderRadius: 14, border: "1px solid var(--border2)", color: TREND[String(v)] || "var(--muted)" }}>{k}: {String(v)}</span>))}
              </div>
            </Panel>
            <Panel id="ch-levels" title="Support and resistance" hint="nearest first">
              <table style={{ width: "100%", fontSize: 13.5 }}><tbody>
                {[...(d.levels || [])].sort((a: any, b: any) => Math.abs(a.distance) - Math.abs(b.distance)).slice(0, 10).map((l: any, i: number) => (
                  <tr key={i}><td>{l.label}</td><td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{num(l.price, 2)}</td>
                    <td style={{ textAlign: "right" }} className={l.distance > 0 ? "up" : "down"}>{pct(l.distance, 2)}</td></tr>))}
              </tbody></table>
            </Panel>
            <Panel id="ch-trades" title="Bot's trades today">
              {d.trades?.length ? <ul style={{ margin: 0, paddingLeft: 18 }}>{d.trades.map((t: any, i: number) => (
                <li key={i}>{t.t} {t.side === "BUY" ? "bought" : "sold"} {t.qty} @ {num(t.price, 2)}{t.profit != null && <b className={t.profit >= 0 ? "up" : "down"}> {t.profit >= 0 ? "+" : "−"}₹{num(Math.abs(t.profit), 2)}</b>}</li>))}</ul>
                : <p className="dim" style={{ margin: 0 }}>No trades in this stock today.</p>}
            </Panel>
          </div>
        </div>)}
    </>
  );
}
