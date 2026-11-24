"use client";
/* Fundamental Analysis Agent (agents/fundamental.py), from /api/fundamental and /api/fundamental/test.
   Company results, news tone, economic events, crowd emotion and behavioural patterns, each with its evidence and the
   time it became public. Observation only: the agent never places, sizes or blocks a trade; a lens earns a role only
   by beating a placebo (shifted-in-time) version of itself on the paper account's trades. */
import { useState } from "react";
import { DataTable, Err, Metric, Panel, Skeleton } from "@/components/ui";
import { num, pct, useApi } from "@/lib/api";

const LENS: [string, string][] = [["reports", "Company reports"], ["news", "News tone"], ["events", "Economic events"],
  ["emotion", "Crowd emotion"], ["behaviour", "Behaviour patterns"]];
const MOOD_TONE: Record<string, string> = { fear: "var(--crit)", neutral: "var(--muted)", "greed / overconfidence": "var(--warn)" };
const score = (x: any) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${num(Math.abs(x), 2)}`);
const tone = (x: any) => (x == null ? undefined : x >= 0.25 ? "up" : x <= -0.25 ? "down" : undefined);

export default function Fundamental() {
  const { data, error } = useApi<any>("/fundamental", 300000);
  const test = useApi<any>("/fundamental/test", 600000);
  const [pick, setPick] = useState<string>("");
  const stocks = Object.entries(data?.stocks || {}).map(([sym, x]: [string, any]) => ({ sym, ...x }));
  const cur = stocks.find((x) => x.sym === pick) || stocks[0];
  const m = data?.market?.emotion;
  const ev = m?.evidence || {};
  return (
    <>
      <h1>Fundamental Agent</h1>
      <p className="sub">Beyond the chart: company results, news, economic events, crowd emotion and behavioural patterns, each with the
        evidence and the time it became public. Observation only: it does not place, size or block any trade until a lens proves itself.</p>
      {error && <Err e={error} />}
      {!data && !error && <Skeleton lines={6} />}
      {data && (
        <>
          <section className="panel" aria-labelledby="mood-h" data-testid="fa-mood" style={{ marginBottom: 14 }}>
            <div className="ph"><h2 id="mood-h">Market mood</h2><span className="hint">as of {String(data.as_of).replace("T", " ")} IST</span></div>
            <div className="pb">
              <p style={{ margin: "0 0 12px", fontSize: 17 }}>
                <b style={{ color: MOOD_TONE[m?.mood] || "var(--ink)" }}>{m?.mood || "unknown"}</b>
                <span className="ink2"> {String(m?.plain || "").replace(/^Market mood: [^(.]*/, "")}</span></p>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
                <Metric sm k="India VIX (vs last year)" v={ev.india_vix != null ? `${num(ev.india_vix, 1)} · ${pct(ev.vix_percentile_1y, 0)}` : "not imported"} />
                <Metric sm k="Above 20-day average" v={pct(ev.above_20d_average, 0)} />
                <Metric sm k="Panic selling" v={pct(ev.panic_selling_share, 0)} h="big fall on heavy volume" />
                <Metric sm k="Euphoric buying" v={pct(ev.euphoric_buying_share, 0)} h="big jump on heavy volume" />
                <Metric sm k="Near 52-week high" v={pct(ev.near_52w_high_share, 0)} />
                <Metric sm k="Herding (20-day)" v={ev.herding_correlation_20d == null ? "—" : num(ev.herding_correlation_20d, 2)} h="average correlation" />
              </div>
              <p className="dim" style={{ fontSize: 12.5, margin: "10px 0 0" }}>{data.market?.events?.plain}</p>
            </div>
          </section>

          <Panel id="fa-proof" title="Has it earned a say?" hint={data.snapshots ? `${data.snapshots.days} daily snapshots since ${String(data.snapshots.first).slice(0, 10)}` : "no snapshots yet"}>
            <p className="ink2" style={{ marginTop: 0 }}>Each evening the agent's view is saved. A lens counts only if paper trades entered when it was positive did better
              than when it was negative, by more than the same scores shifted in time (a placebo): at least {test.data?.min_trades ?? 100} trades
              and {test.data?.min_days ?? 20} days.</p>
            <div data-testid="fa-proof" style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              {["view", "reports", "news", "behaviour", "emotion", "event_risk"].map((k) => {
                const v = test.data?.lenses?.[k]?.verdict || test.data?.verdict || "not enough evidence";
                return <span key={k} className="chip" style={{ padding: "4px 10px", border: "1px solid var(--border2)", borderRadius: 14, fontSize: 13,
                  color: v.startsWith("informative") ? "var(--good)" : v.startsWith("no usable") ? "var(--crit)" : "var(--muted)" }}>{k.replace("_", " ")}: {v}</span>;
              })}
            </div>
          </Panel>

          <div className="grid g2" style={{ marginTop: 14, alignItems: "start" }}>
            <Panel id="fa-stocks" title="Watchlist" hint="click a stock for its evidence">
              <DataTable rows={stocks} onRow={(r: any) => setPick(r.sym)} selected={(r: any) => r.sym === cur?.sym} maxHeight={520} cols={[
                { key: "sym", label: "Stock" },
                { key: "stance", label: "Agent's view", f: (r: any) => <span className={tone(r.view)}>{r.stance}</span> },
                { key: "reports", label: "Reports", r: true, f: (r: any) => score(r.lenses.reports.score), sort: (r: any) => r.lenses.reports.score ?? -9 },
                { key: "news", label: "News", r: true, f: (r: any) => score(r.lenses.news.score), sort: (r: any) => r.lenses.news.score ?? -9 },
                { key: "behaviour", label: "Behaviour", r: true, f: (r: any) => score(r.lenses.behaviour.score), sort: (r: any) => r.lenses.behaviour.score ?? -9 },
                { key: "risk", label: "Event risk", r: true, f: (r: any) => (r.lenses.events.risk ? num(r.lenses.events.risk, 1) : "—") },
              ]} />
            </Panel>
            {cur && (
              <Panel id="fa-detail" title={cur.sym} hint={cur.stance}>
                <div data-testid="fa-detail">
                  {LENS.map(([k, label]) => {
                    const L = cur.lenses[k] || {};
                    return (
                      <div key={k} style={{ padding: "8px 0", borderTop: "1px solid var(--border2)" }}>
                        <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                          <b>{label}</b>
                          <span className={tone(L.score)} style={{ fontVariantNumeric: "tabular-nums" }}>
                            {L.status === "unavailable" ? "unavailable" : k === "events" ? `risk ${num(L.risk ?? 0, 1)}` : score(L.score)}</span>
                        </div>
                        <p className="ink2" style={{ margin: "4px 0 0", fontSize: 14 }}>{L.plain || L.reason}</p>
                        {k === "news" && L.evidence?.latest?.length > 0 && (
                          <ul className="dim" style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 12.5 }}>
                            {L.evidence.latest.map((h: any, i: number) => <li key={i}>{h.title} <span>({String(h.first_seen).slice(0, 16).replace("T", " ")}, {h.source})</span></li>)}
                          </ul>)}
                      </div>);
                  })}
                </div>
              </Panel>)}
          </div>
          {data.notes?.length > 0 && (
            <Panel id="fa-notes" title="Missing inputs" hint="the agent never fills a gap by guessing">
              <ul style={{ margin: 0, paddingLeft: 20 }} data-testid="fa-notes">{data.notes.map((n: string) => <li key={n} className="ink2">{n}</li>)}</ul>
            </Panel>)}
        </>)}
    </>
  );
}
