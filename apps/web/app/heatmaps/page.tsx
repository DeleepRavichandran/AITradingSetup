"use client";
/* Heatmap Intelligence (services/screening/heatmaps.py, feature_study.py). Sixteen maps from data this lab actually has;
   maps the broker cannot supply (order-book depth, options open interest, order flow) say so and draw nothing. Every map
   shows its source, timestamp and freshness; playback recomputes it from data at or before the chosen time. Stock tiles
   and rows open the decision dossier and the chart. Features behind the maps are tested before they may matter. */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Dossier, Tag, signedPct, statusTone } from "@/components/vega";
import { useApi, useQuery } from "@/lib/api";

const FEATURE_OF: Record<string, string[]> = { relative_strength: ["rs_60", "rs_day"], sector_rotation: ["sector_rs_60"], volume: ["rel_volume"],
  volatility: ["vol_change"], inferred_liquidity: ["liq_above", "liq_below"], market_cap: ["rs_day", "mom_60"] };
const AV: Record<string, [string, string]> = { available: ["ok", "data"], limited: ["wait", "limited"], unavailable: ["no", "unavailable"] };

function fmt(v: number | null, unit: string) {
  if (v == null) return "—";
  if (/%|share|return/.test(unit) && Math.abs(v) < 5) return `${v >= 0 && /vs|change|return/.test(unit) ? "+" : ""}${(v * 100).toFixed(Math.abs(v) < 0.1 ? 2 : 1)}%`;
  if (/correlation/.test(unit)) return v.toFixed(2);
  if (Math.abs(v) >= 100) return Math.round(v).toLocaleString("en-IN");
  return v.toFixed(2);
}

function cellColor(v: number | null, max: number, diverging: boolean) {
  if (v == null || !max) return "var(--sunk)";
  const k = Math.min(1, Math.abs(v) / max);
  const c = diverging ? (v >= 0 ? "var(--good)" : "var(--crit)") : "var(--data)";
  return `color-mix(in srgb, ${c} ${Math.round(8 + k * 40)}%, var(--surface))`;
}

function Grid({ h, onRow }: { h: any; onRow?: (r: string) => void }) {
  const div = h.scale === "diverging";
  const flat = h.values.flat().filter((v: any) => v != null && Number.isFinite(v)).map((v: number) => Math.abs(h.id === "correlation" ? v : v));
  const max = h.id === "correlation" ? 1 : flat.length ? flat.sort((a: number, b: number) => a - b)[Math.floor(flat.length * 0.95)] || Math.max(...flat) : 0;
  return (
    <div className="hm-wrap" tabIndex={0} aria-label={`${h.title} heatmap (scrollable)`}>
      <div className="hm" style={{ gridTemplateColumns: `minmax(110px, max-content) repeat(${h.cols.length}, minmax(58px, 1fr))` }} data-testid="hm-grid">
        <div className="hm-c hm-h" />
        {h.cols.map((c: string) => <div key={c} className="hm-c hm-h" title={c}>{c}</div>)}
        {h.rows.map((r: string, i: number) => (
          <div key={r} style={{ display: "contents" }}>
            {onRow ? <button className="hm-c hm-r" onClick={() => onRow(r)} title={`Open ${r}`}>{r}{h.verdicts?.[r] && <span className="st">{h.verdicts[r]}</span>}</button>
              : <div className="hm-c hm-r">{r}</div>}
            {h.values[i].map((v: number | null, j: number) => (
              <div key={j} className="hm-c hm-v" style={{ background: cellColor(h.id === "correlation" && i === j ? null : v, max, div) }}
                title={`${r} · ${h.cols[j]}: ${fmt(v, h.unit)}${h.counts ? ` (${h.counts[i][j]} trades)` : ""}`}>{fmt(v, h.unit)}</div>))}
          </div>))}
      </div>
    </div>
  );
}

function Treemap({ tiles, onTile }: { tiles: any[]; onTile: (s: string) => void }) {
  const bySector = useMemo(() => {
    const m: Record<string, any[]> = {};
    tiles.forEach((t) => (m[t.sector] = m[t.sector] || []).push(t));
    return Object.entries(m).map(([s, ts]) => ({ sector: s, tiles: ts, size: ts.reduce((a, t) => a + Math.max(t.size, 1), 0) })).sort((a, b) => b.size - a.size);
  }, [tiles]);
  const total = bySector.reduce((a, s) => a + s.size, 0) || 1;
  const max = Math.max(0.005, ...tiles.map((t) => Math.abs(t.value ?? 0)));
  return (
    <div className="tm" data-testid="treemap">
      {bySector.map((s) => (
        <div key={s.sector} className="tm-s" style={{ flexGrow: s.size / total, flexBasis: 0 }}>
          <div className="tm-sl">{s.sector}</div>
          <div className="tm-col">{s.tiles.map((t) => (
            <button key={t.symbol} className="tm-t" style={{ flexGrow: Math.max(t.size, 1) / s.size, background: cellColor(t.value, max, true) }}
              onClick={() => onTile(t.symbol)} aria-label={`${t.symbol} ${signedPct(t.value)}: open dossier`}>
              <b>{t.symbol}</b><span>{signedPct(t.value)}</span></button>))}</div>
        </div>))}
    </div>
  );
}

export default function Heatmaps() {
  const q = useQuery();
  const cat = useApi<any>("/heatmaps", 300000);
  const [id, setId] = useState("market_cap");
  const [tf, setTf] = useState("1d");
  const [sector, setSector] = useState("");
  const [day, setDay] = useState("");
  const [time, setTime] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [inspect, setInspect] = useState<string | null>(null);
  useEffect(() => { const m = q.get("map"); if (m) setId(m); }, [q]);
  const at = day && time ? `${day}T${time}` : "";
  const h = useApi<any>(`/heatmaps/${id}?tf=${tf}${sector ? `&sector=${encodeURIComponent(sector)}` : ""}${at ? `&at=${at}` : ""}`, at ? 0 : 60000);
  const d = h.data, c = cat.data;
  const feats = (c?.features?.results || []).filter((r: any) => (FEATURE_OF[id] || []).includes(r.feature));
  const stockRows = d && ["relative_strength", "volume", "volatility", "opportunity", "portfolio_risk", "inferred_liquidity", "news", "data_quality"].includes(id);
  const groups = useMemo(() => {
    const g: Record<string, any[]> = {};
    (c?.catalog || []).forEach((m: any) => (g[m.group] = g[m.group] || []).push(m));
    return Object.entries(g);
  }, [c]);
  const fresh = d?.as_of && d?.generated_at ? Math.max(0, (new Date(d.generated_at).getTime() - new Date(String(d.as_of).replace(" ", "T")).getTime()) / 60000) : null;
  return (
    <>
      <div className="vhead"><div><h1>Heatmap Intelligence</h1><p>Where strength, volume, risk and evidence concentrate, from data this lab actually holds. What the feed cannot show is marked, not drawn.</p></div></div>
      <div className="hm-layout">
        <nav className="vp hm-nav" aria-label="Heatmaps">
          {groups.map(([g, ms]) => <div key={g}><div className="navg" style={{ textTransform: "capitalize" }}>{g}</div>
            {ms.map((m: any) => <button key={m.id} className={m.id === id ? "on" : ""} aria-pressed={m.id === id} onClick={() => { setId(m.id); setInspect(null); }}>
              <i className={`dot-${m.availability}`} />{m.title}</button>)}</div>)}
        </nav>
        <div className="vcol">
          <div className="row" style={{ alignItems: "end" }} role="group" aria-label="Heatmap controls">
            <label>Timeframe<select value={tf} onChange={(e) => setTf(e.target.value)} disabled={id !== "market_cap"}>{(c?.timeframes || ["1d"]).map((t: string) => <option key={t}>{t}</option>)}</select></label>
            <label>Sector<select value={sector} onChange={(e) => setSector(e.target.value)}><option value="">All sectors</option>{(c?.sectors || []).map((s: string) => <option key={s}>{s}</option>)}</select></label>
            <label>Play back: day<input type="date" value={day} onChange={(e) => setDay(e.target.value)} /></label>
            <label>time<input type="time" min="09:15" max="15:30" value={time} onChange={(e) => setTime(e.target.value)} /></label>
            {at && <button className="ghost" onClick={() => { setDay(""); setTime(""); }}>Back to live</button>}
          </div>
          {h.error && <div className="banner crit">Could not load: {h.error}</div>}
          {d && <section className="vp" aria-labelledby="hm-title">
            <div className="h"><div><h2 id="hm-title">{d.title}</h2><div className="sub2">{d.unit || ""}</div></div>
              <Tag tone={AV[d.availability]?.[0] || ""} testid="hm-availability">{AV[d.availability]?.[1] || d.availability}</Tag></div>
            <div className="b">
              {d.availability === "unavailable" ? <div className="unavail" data-testid="hm-unavailable"><b>Not available from this lab's data</b>{d.why}</div>
                : !d.rows?.length ? <p className="empty">No data for this view yet.</p>
                : id === "market_cap" ? <Treemap tiles={d.tiles || []} onTile={setOpen} />
                : <Grid h={d} onRow={stockRows ? (r) => (id === "inferred_liquidity" ? setInspect(r) : setOpen(r)) : undefined} />}
              {d.availability !== "unavailable" && d.rows?.length > 0 && <div className="legend" style={{ marginTop: 10 }}>
                {d.scale === "diverging" ? <><span><i style={{ background: "var(--crit)" }} />negative</span><span><i style={{ background: "var(--sunk)" }} />near zero / no data</span><span><i style={{ background: "var(--good)" }} />positive</span></>
                  : <><span><i style={{ background: "var(--sunk)" }} />low / no data</span><span><i style={{ background: "var(--data)" }} />high</span></>}
                <span>colour saturates at the 95th percentile</span></div>}
              {inspect && d.levels?.[inspect] && <div style={{ marginTop: 12 }} data-testid="hm-levels">
                <h3 style={{ marginTop: 0 }}>{inspect}: inferred liquidity levels (from candles, not resting orders)</h3>
                <div className="vlist">{d.levels[inspect].sort((a: any, b: any) => a.distance - b.distance).map((l: any, i: number) =>
                  <div key={i}><span>{l.kind} <span className="dim">({l.side})</span></span><b>{l.price} · {signedPct(l.distance)}</b></div>)}</div>
                <p style={{ margin: "8px 0 0" }}><Link href={`/charts?symbol=${inspect}`}>Open {inspect} in Chart Analyst with ICT/SMC overlays</Link></p></div>}
            </div>
            <div className="ft" data-testid="hm-provenance"><span>Source: {d.source || "—"}{d.data_source?.label ? ` · prices: ${d.data_source.label}` : ""}</span>
              <span>{d.as_of ? `As of ${String(d.as_of).replace("T", " ").slice(0, 16)}` : ""}{fresh != null ? ` · ${fresh < 2 ? "fresh" : `${Math.round(fresh)} min old`}` : ""}{at ? " · playback" : ""}</span></div>
            {d.limitations?.length > 0 && <div className="ft"><span>{d.limitations.join(" ")}</span></div>}
          </section>}
          {d && d.availability !== "unavailable" && <section className="vp" aria-labelledby="ev-h">
            <div className="h"><div><h2 id="ev-h">Does this map predict anything?</h2><div className="sub2">Its features against the next 30 minutes, beyond the stock's own momentum, after costs: development selects (day-clustered), the sealed holdout is read once</div></div></div>
            {feats.length ? <div className="tw"><table data-testid="hm-features"><thead><tr><th>Feature</th><th className="r">Sample times</th><th className="r">Mean IC</th><th className="r">Beyond momentum</th><th className="r">Holdout IC</th><th className="r">Top third, net</th><th>Status</th></tr></thead>
              <tbody>{feats.map((r: any) => <tr key={r.feature}><td>{r.feature}</td><td className="r">{r.times}</td><td className="r">{r.mean_ic == null ? "—" : r.mean_ic.toFixed(3)}</td>
                <td className="r">{r.incremental_mean_ic == null ? "—" : r.incremental_mean_ic.toFixed(3)}</td><td className="r">{r.holdout_mean_ic == null ? "—" : r.holdout_mean_ic.toFixed(3)}</td>
                <td className="r">{signedPct(r.top_third_net, 3)}</td><td><Tag tone={statusTone(r.verdict)}>{r.verdict}</Tag>{r.reason && <span className="st" style={{ whiteSpace: "normal", maxWidth: 240 }}>{r.reason}</span>}</td></tr>)}</tbody></table></div>
              : <p className="empty">{FEATURE_OF[id] ? "No feature study yet: it runs with each daily review." : "This map is descriptive; no feature from it is tested yet."}</p>}
            <div className="ft"><span>Means are development figures; holdout figures appear only for development survivors. No feature influences a trade until it is holdout- and forward-supported and approved.</span></div>
          </section>}
        </div>
      </div>
      <Dossier symbol={open} onClose={() => setOpen(null)} />
    </>
  );
}
