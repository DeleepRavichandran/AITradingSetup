"use client";
/* Strategy Universe: every registered configuration, aggregated (family x timeframe map, funnel, return
   distribution) and explorable (server-side paging + windowed table + dossier). Nothing renders 50,000 DOM
   nodes: the map has 12 cells, the table renders only visible rows of a server page. */
import Link from "next/link";
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Col, Err, Funnel, KV, Metric, Panel, Prov, Segmented, Skeleton, Unavailable, VirtualTable } from "@/components/ui";
import { api, inr, num, useApi } from "@/lib/api";

const STAGES: [string, string][] = [["", "All stages"], ["qualified", "Qualified"], ["forward_evidence", "Some forward trades"], ["gated", "Proven negative"],
  ["evaluated", "Evaluated, no trade"], ["registered", "Not yet evaluated"]];
const STAGE_TONE: Record<string, string> = { qualified: "good", forward_evidence: "info", gated: "crit", evaluated: "", registered: "" };
const SORTS: [string, string][] = [["trades", "Most forward trades"], ["mean_net", "Mean return after costs"], ["realised", "Realised ₹"], ["confidence", "Confidence"], ["evaluations", "Evaluations"]];
const PAGE = 500;

const cellColor = (bps: number | null) => {
  if (bps == null) return "var(--raised)";
  const a = Math.min(Math.abs(bps) / 40, 1);
  return `color-mix(in oklab, ${bps >= 0 ? "var(--good)" : "var(--crit)"} ${Math.round(12 + a * 48)}%, var(--raised))`;
};

function Histogram({ h }: { h: any }) {
  if (!h) return <Unavailable title="Not enough completed trades">A distribution needs at least 5 configurations with a completed forward trade.</Unavailable>;
  const max = Math.max(...h.counts, 1), W = 600, H = 130, bw = W / h.counts.length;
  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Distribution of mean forward return per configuration, ${h.configs} configurations`} style={{ width: "100%", height: "auto" }}>
        {h.counts.map((c: number, i: number) => {
          const lo = h.edges_bps[i], bh = (c / max) * H;
          return <rect key={i} x={i * bw + 1} y={H - bh} width={bw - 2} height={bh} rx={2} fill={lo < 0 ? "var(--crit)" : "var(--good)"} opacity={0.75}>
            <title>{`${lo} to ${h.edges_bps[i + 1]} bps: ${c}`}</title></rect>;
        })}
        <line x1={W / 2} x2={W / 2} y1={0} y2={H} stroke="var(--axis)" strokeDasharray="3 3" />
      </svg>
      <div className="dim" style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }} aria-hidden="true">
        <span>{h.edges_bps[0]} bps</span><span>0</span><span>+{h.edges_bps[h.edges_bps.length - 1]} bps</span></div>
      <figcaption className="dim" style={{ fontSize: 12.5 }}>Mean net return per trade, {h.configs} configurations with forward trades; clipped at ±{h.clipped_at_bps} bps.</figcaption>
    </figure>
  );
}

function Dossier({ id, onClose }: { id: string; onClose: () => void }) {
  const d = useApi<any>(`/universe/configs/${id}`);
  const x = d.data;
  return (
    <Panel title={<>Dossier · <span className="mono">{id}</span></>} id="dossier" right={<button onClick={onClose} aria-label="Close dossier">Close</button>}
      prov={x && <Prov source="screening registry, forward paper ledger, signal log" data={x.data_provenance} />}>
      <Err e={d.error} />{d.loading && <Skeleton lines={6} />}
      {x && <div className="stack" data-testid="dossier">
        <div>
          <p className="lede" style={{ fontSize: 18 }}>{x.config.rule} · {x.config.symbol} · {x.config.timeframe} ({x.config.horizon})</p>
          <p className="ink2" style={{ margin: "6px 0 0" }}>{x.config.definition}</p>
          <p style={{ margin: "8px 0 0" }}><Badge s={x.stage.replaceAll("_", " ")} tone={STAGE_TONE[x.stage]} /> <Badge s={`position ${x.position}`} /></p>
        </div>
        <div className="metrics">
          <Metric sm k="Forward trades" v={x.forward_evidence.trades} h={`${x.forward_evidence.wins} wins`} />
          <Metric sm k="Mean after costs" v={x.forward_evidence.mean_net_bps == null ? "—" : `${num(x.forward_evidence.mean_net_bps, 1)} bps`}
            tone={x.forward_evidence.mean_net_bps == null ? undefined : x.forward_evidence.mean_net_bps >= 0 ? "up" : "down"} h={`shrunk ${num(x.forward_evidence.shrunk_mean_bps, 1)} bps`} />
          <Metric sm k="Confidence" v={num(x.forward_evidence.confidence, 2)} h={`P(win) ${num(x.forward_evidence.win_probability, 2)}`} />
          <Metric sm k="Realised" v={inr(x.forward_evidence.realised_after_charges_inr)} h={`charges ${inr(x.forward_evidence.charges_inr)}`} />
        </div>
        <div>
          <h3>Known weaknesses</h3>
          {x.weaknesses.length ? <ul className="list">{x.weaknesses.map((w: string) => <li key={w} className="ink2">{w}</li>)}</ul> : <p className="dim">None flagged.</p>}
        </div>
        <div>
          <h3>Evidence by kind</h3>
          <KV items={[
            ["Historical backtest", <>{x.historical_backtest.status} · <span className="dim">{x.historical_backtest.how}</span></>],
            ["Walk-forward", x.walk_forward.status],
            ["Out-of-sample", <>{x.out_of_sample.status} · <span className="dim">{x.out_of_sample.note}</span></>],
            ["Paper trading", `${x.forward_evidence.trades} completed round trips, ${x.forward_evidence.evaluations.toLocaleString("en-IN")} evaluations`],
            ["BUY signals", Object.entries(x.forward_evidence.buy_signals_by_allocation).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"],
          ]} />
        </div>
        <div>
          <h3>Costs and execution assumptions</h3>
          <KV items={[
            ["Product", x.costs.product], ["Charges", `${x.costs.schedule}; paid ${inr(x.costs.charges_paid_inr)}`],
            ["Fills", x.execution.settings ? `${x.execution.model}: ≤${num(x.execution.settings.participation * 100, 0)}% of bar volume, ${x.execution.settings.half_spread_bps} bps half-spread + ${x.execution.settings.impact_bps} bps × √(q/V) impact, entries expire after ${x.execution.settings.entry_ttl_minutes} min` : x.execution.model],
            ["Capital", x.execution.portfolio_rules ? `shared pot ${inr(x.execution.portfolio_rules.capital)}, ${num(x.execution.portfolio_rules.per_trade_pct * 100, 0)}% per trade, allocation ${x.execution.allocation}` : "own sub-account"],
          ]} />
        </div>
        <div>
          <h3>Recent signals and allocation decisions</h3>
          {x.recent_signals.length ? <ul className="list">{x.recent_signals.slice(0, 8).map((s: any, i: number) => <li key={i}>
            <span className="mono">{String(s.bar_ts).replace("T", " ").slice(0, 16)}</span> <Badge s={s.decision} tone={s.decision === "BUY" ? "good" : "warn"} />
            {s.alloc_status && <> <Badge s={s.alloc_status} tone={s.alloc_status === "accepted" ? "good" : s.alloc_status === "deferred" ? "warn" : "crit"} /></>}
            <div className="d">{s.alloc_reason || s.reason}</div></li>)}</ul> : <p className="dim">No actionable signals yet.</p>}
        </div>
        <div className="grid g2">
          <div><h3>Same rule, other symbols</h3><Related rows={x.related.same_rule_other_symbols} /></div>
          <div><h3>Same symbol, other rules</h3><Related rows={x.related.same_symbol_other_rules} /></div>
        </div>
        <div>
          <h3>AI research notes ({x.config.family})</h3>
          {x.ai_research_notes.length ? <ul className="list">{x.ai_research_notes.slice(0, 4).map((n: any, i: number) => <li key={i}><Badge s={n.outcome} /> <span className="t">{n.text}</span>{n.next_step && <div className="d">Next: {n.next_step}</div>}</li>)}</ul>
            : <p className="dim">The research agents have recorded nothing about this family yet.</p>}
        </div>
        <div>
          <h3>Version history</h3>
          {x.version_history.map((v: any) => <p key={v.version} className="ink2" style={{ fontSize: 13.5 }}>v{v.version} · <span className="mono">{v.id}</span>: {v.note}.</p>)}
        </div>
      </div>}
    </Panel>
  );
}

const Related = ({ rows }: { rows: any[] }) => rows.length ? (
  <ul className="list">{rows.map((r) => <li key={r.id}><Link href={`/universe?config=${r.id}`} className="mono">{r.id}</Link> <span className="ink2">{r.symbol} {r.rule} {r.timeframe}</span>
    <div className="d">{r.stage.replaceAll("_", " ")} · {r.trades} trades{r.mean_net_bps != null ? ` · ${num(r.mean_net_bps, 1)} bps` : ""}</div></li>)}</ul>
) : <p className="dim">None.</p>;

export default function Universe() {
  const [f, setF] = useState({ family: "", timeframe: "", horizon: "", stage: "", q: "", sort: "trades" });
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const sm = useApi<any>("/universe/summary", 30000);
  const qs = useMemo(() => new URLSearchParams({ ...f, desc: "true", limit: String(PAGE) }).toString(), [f]);
  const load = useCallback(async (offset: number) => {
    setLoading(true); setErr(null);
    try {
      const d = await api(`/universe/configs?${qs}&offset=${offset}`);
      setRows((r) => (offset ? [...r, ...d.rows] : d.rows)); setTotal(d.total);
    } catch (e: any) { setErr(e.message); } finally { setLoading(false); }
  }, [qs]);
  useEffect(() => { load(0); }, [load]);
  useEffect(() => {                                                    // ?config=<id> opens a dossier (links from Vega and elsewhere)
    const c = new URLSearchParams(window.location.search).get("config");
    if (c) setSel(c);
  }, []);
  const open = (id: string | null) => {
    setSel(id);
    const u = new URL(window.location.href);
    if (id) u.searchParams.set("config", id); else u.searchParams.delete("config");
    window.history.replaceState(null, "", u.toString());
    if (id) setTimeout(() => document.getElementById("dossier")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };
  const s = sm.data;
  const fams = s ? [...new Set<string>(s.map.map((c: any) => c.family))] : [];
  const tfs = ["1m", "5m", "15m", "1d"];
  const cols: Col[] = [
    { key: "id", label: "configuration", f: (r) => <span className="mono">{r.id}</span> },
    { key: "symbol", label: "symbol" }, { key: "rule", label: "rule" }, { key: "timeframe", label: "tf" },
    { key: "stage", label: "stage", f: (r) => <Badge s={r.stage.replaceAll("_", " ")} tone={STAGE_TONE[r.stage]} /> },
    { key: "trades", label: "trades", r: true }, { key: "mean_net_bps", label: "mean after costs", r: true, f: (r) => r.mean_net_bps == null ? <span className="dim">—</span>
      : <span className={r.mean_net_bps >= 0 ? "up" : "down"}>{num(r.mean_net_bps, 1)} bps</span> },
    { key: "confidence", label: "conf.", r: true, f: (r) => num(r.confidence, 2) },
    { key: "realised_inr", label: "realised", r: true, f: (r) => inr(r.realised_inr) },
    { key: "position", label: "position" },
  ];
  return (
    <>
      <div className="top"><h1>Strategy Universe</h1></div>
      <p className="sub">Every configuration the one-minute loop evaluates, grouped by rule family and timeframe, with its forward paper evidence. Select one to open its research dossier.</p>
      <Err e={sm.error && !sm.error.includes("no screening run") ? sm.error : null} />
      {sm.error?.includes("no screening run") && <Unavailable title="No screening run yet">Start a replay on the <Link href="/screening">Intraday &amp; Swing Lab</Link> or the live Angel One feed; the universe is built from the registered configurations.</Unavailable>}
      {!s && !sm.error && <Skeleton lines={5} />}
      {s && <>
        <div className="metrics" style={{ marginBottom: 6 }}>
          <Metric k="Configurations" v={s.registered.toLocaleString("en-IN")} h={`${s.symbols} symbols`} />
          <Metric k="Independent rules" v={s.rules} h={`${s.families} families: ${Object.entries(s.rules_by_family).map(([k, v]) => `${k} ${v}`).join(", ")}`} />
          <Metric k="With forward trades" v={(s.by_stage.forward_evidence + s.by_stage.qualified + s.by_stage.gated).toLocaleString("en-IN")} />
          <Metric k="Qualified" v={s.by_stage.qualified} tone={s.by_stage.qualified ? "up" : undefined} h={`${s.by_stage.gated} proven negative`} />
        </div>
        <p className="dim" style={{ fontSize: 13.5, margin: "0 0 14px" }}>{s.note}</p>
        <div className="grid g21" style={{ marginBottom: 14 }}>
          <Panel title="Family × timeframe map" hint="colour: mean forward return after costs · click to filter" id="map"
            prov={<Prov source="screening registry + forward paper ledger" note={`computed in ${s.compute_ms} ms`} />}>
            <div className="matrix" style={{ gridTemplateColumns: `90px repeat(${tfs.length}, minmax(0, 1fr))` }} role="group" aria-label="Filter by family and timeframe">
              <span />{tfs.map((t) => <span key={t} className="lab" style={{ textAlign: "center" }}>{t}{t === "1d" ? " (swing)" : ""}</span>)}
              {fams.map((fam) => <Fragment key={fam}>
                <span className="lab">{fam}</span>
                {tfs.map((t) => {
                  const c = s.map.find((x: any) => x.family === fam && x.timeframe === t);
                  if (!c) return <span key={fam + t} />;
                  const on = f.family === fam && f.timeframe === t;
                  return <button key={fam + t} className="cell" aria-pressed={on} style={{ background: cellColor(c.mean_net_bps) }}
                    onClick={() => setF({ ...f, family: on ? "" : fam, timeframe: on ? "" : t })}
                    aria-label={`${fam} ${t}: ${c.configs} configurations, ${c.with_evidence} with trades, mean ${c.mean_net_bps == null ? "none" : num(c.mean_net_bps, 1) + " bps"}`}>
                    <div className="a">{c.mean_net_bps == null ? "—" : `${num(c.mean_net_bps, 1)}`}<span style={{ fontSize: 12, fontWeight: 400 }}>{c.mean_net_bps == null ? "" : " bps"}</span></div>
                    <div className="b">{c.configs} configs · {c.trades} trades · {c.qualified} qualified</div>
                  </button>;
                })}
              </Fragment>)}
            </div>
          </Panel>
          <Panel title="Validation funnel" id="funnel" prov={<Prov source="screening registry + signal log" />}>
            <Funnel steps={s.funnel} />
            <h3>Return distribution</h3>
            <Histogram h={s.return_distribution} />
          </Panel>
        </div>
        <Panel title="Configurations" hint={`${total.toLocaleString("en-IN")} match · showing ${rows.length.toLocaleString("en-IN")}`} id="table"
          prov={<Prov source="/api/universe/configs (server-side filter, sort and paging)" />}>
          <div className="row" style={{ marginBottom: 10 }}>
            <Segmented label="Horizon" value={f.horizon} options={[["", "All"], ["intraday", "Intraday"], ["swing", "Swing"]]} onChange={(v) => setF({ ...f, horizon: v })} />
            <label>Stage<select value={f.stage} onChange={(e) => setF({ ...f, stage: e.target.value })}>{STAGES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
            <label>Sort<select value={f.sort} onChange={(e) => setF({ ...f, sort: e.target.value })}>{SORTS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
            <label>Search<input value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} placeholder="symbol, rule or id" /></label>
            {(f.family || f.timeframe) && <button onClick={() => setF({ ...f, family: "", timeframe: "" })}>Clear map filter ({f.family} {f.timeframe})</button>}
          </div>
          <Err e={err} />
          <VirtualTable cols={cols} rows={rows} height={460} onRow={(r) => open(r.id)} selected={(r) => r.id === sel} empty={loading ? "Loading…" : "No configuration matches."} />
          {rows.length < total && <button style={{ marginTop: 8 }} disabled={loading} onClick={() => load(rows.length)}>{loading ? "Loading…" : `Load ${Math.min(PAGE, total - rows.length)} more`}</button>}
        </Panel>
        {sel && <div style={{ marginTop: 14 }}><Dossier id={sel} onClose={() => open(null)} /></div>}
      </>}
    </>
  );
}
