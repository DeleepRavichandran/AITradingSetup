"use client";
import React, { useMemo, useRef, useState } from "react";

const W = 800;
const SER = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
export const seriesColor = (i: number) => SER[i % SER.length];

function ticks(lo: number, hi: number, n = 4): number[] {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}
const compact = (v: number) => {
  const a = Math.abs(v);
  return a >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : a >= 1e5 ? `${(v / 1e5).toFixed(2)}L` : a >= 1000 ? `${(v / 1000).toFixed(1)}k` : a >= 10 ? v.toFixed(0) : v.toFixed(2);
};

export type Series = { name: string; values: (number | null)[] };

/** Multi-series line chart on ONE shared axis, with crosshair + tooltip. */
export function LineChart({ x, series, height = 240, fmt = compact, area = false, zero = false }: {
  x: string[]; series: Series[]; height?: number; fmt?: (v: number) => string; area?: boolean; zero?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [hi, setHi] = useState<number | null>(null);
  const m = { l: 56, r: 12, t: 8, b: 22 };
  const g = useMemo(() => {
    const all = series.flatMap((s) => s.values.filter((v): v is number => v != null && Number.isFinite(v)));
    let lo = Math.min(...all), up = Math.max(...all);
    if (zero) { lo = Math.min(lo, 0); up = Math.max(up, 0); }
    if (!Number.isFinite(lo)) { lo = 0; up = 1; }
    if (lo === up) { lo -= 1; up += 1; }
    const pad = (up - lo) * 0.06;
    return { lo: lo - pad, up: up + pad };
  }, [series, zero]);
  const n = x.length;
  if (n < 2) return <div className="empty">Not enough points to draw.</div>;
  const X = (i: number) => m.l + (i / (n - 1)) * (W - m.l - m.r);
  const Y = (v: number) => m.t + (1 - (v - g.lo) / (g.up - g.lo)) * (height - m.t - m.b);
  const path = (vals: (number | null)[]) => {
    let d = "", pen = false;
    vals.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`; pen = true; });
    return d;
  };
  const move = (e: React.MouseEvent | React.TouchEvent) => {
    const r = ref.current!.getBoundingClientRect();
    const cx = "touches" in e ? e.touches[0].clientX : e.clientX;
    const px = ((cx - r.left) / r.width) * W;
    setHi(Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1)))));
  };
  const xt = [0, Math.floor((n - 1) / 3), Math.floor((2 * (n - 1)) / 3), n - 1];
  return (
    <div className="chart" ref={ref} onMouseMove={move} onMouseLeave={() => setHi(null)} onTouchStart={move} onTouchMove={move}>
      {series.length > 1 && <div className="legend">{series.map((s, i) => <span key={s.name}><i style={{ background: seriesColor(i) }} />{s.name}</span>)}</div>}
      <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label={`Line chart: ${series.map((s) => s.name).join(", ")}`}>
        {ticks(g.lo, g.up).map((t) => (
          <g key={t}><line x1={m.l} x2={W - m.r} y1={Y(t)} y2={Y(t)} stroke="var(--grid)" strokeWidth={1} />
            <text className="axis" x={m.l - 6} y={Y(t) + 4} textAnchor="end">{fmt(t)}</text></g>))}
        {zero && <line x1={m.l} x2={W - m.r} y1={Y(0)} y2={Y(0)} stroke="var(--axis)" strokeWidth={1} />}
        {xt.map((i, k) => <text key={k} className="axis" x={X(i)} y={height - 5} textAnchor={k === 0 ? "start" : k === 3 ? "end" : "middle"}>{String(x[i]).slice(0, 10)}</text>)}
        {area && series.length === 1 && <path d={`${path(series[0].values)}L${X(n - 1)},${Y(Math.max(g.lo, Math.min(0, g.up)))}L${X(0)},${Y(Math.max(g.lo, Math.min(0, g.up)))}Z`} fill={seriesColor(0)} opacity={0.12} />}
        {series.map((s, i) => <path key={s.name} d={path(s.values)} fill="none" stroke={seriesColor(i)} strokeWidth={2} strokeLinejoin="round" />)}
        {hi != null && <line x1={X(hi)} x2={X(hi)} y1={m.t} y2={height - m.b} stroke="var(--axis)" strokeWidth={1} />}
        {hi != null && series.map((s, i) => s.values[hi] != null && <circle key={i} cx={X(hi)} cy={Y(s.values[hi] as number)} r={4} fill={seriesColor(i)} stroke="var(--surface)" strokeWidth={2} />)}
      </svg>
      {hi != null && (
        <div className="tip" style={{ left: `${Math.min(Math.max((X(hi) / W) * 100, 12), 78)}%`, top: 26 }}>
          <b>{String(x[hi]).slice(0, 16)}</b>
          {series.map((s, i) => <div key={s.name}><span><i style={{ background: seriesColor(i) }} />{s.name}</span><span>{s.values[hi] == null ? "—" : fmt(s.values[hi] as number)}</span></div>)}
        </div>)}
    </div>
  );
}

export type Marker = { ts: string; price: number; side: number; label: string };

/** OHLC candles with fill markers. Up/down is encoded by fill (hollow vs solid) AND colour. */
export function Candles({ ts, open, high, low, close, markers = [], height = 300, lines = [] }: {
  ts: string[]; open: number[]; high: number[]; low: number[]; close: number[]; markers?: Marker[]; height?: number; lines?: { price: number; label: string }[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [hi, setHi] = useState<number | null>(null);
  const m = { l: 56, r: 12, t: 10, b: 22 };
  const n = ts.length;
  if (!n) return <div className="empty">No bars.</div>;
  const lo0 = Math.min(...low, ...markers.map((k) => k.price)), up0 = Math.max(...high, ...markers.map((k) => k.price));
  const pad = (up0 - lo0) * 0.06 || 1, lo = lo0 - pad, up = up0 + pad;
  const bw = (W - m.l - m.r) / n;
  const X = (i: number) => m.l + (i + 0.5) * bw;
  const Y = (v: number) => m.t + (1 - (v - lo) / (up - lo)) * (height - m.t - m.b);
  const idx = (t: string) => ts.findIndex((x) => x === t);
  const move = (e: React.MouseEvent) => {
    const r = ref.current!.getBoundingClientRect();
    setHi(Math.max(0, Math.min(n - 1, Math.floor((((e.clientX - r.left) / r.width) * W - m.l) / bw))));
  };
  return (
    <div className="chart" ref={ref} onMouseMove={move} onMouseLeave={() => setHi(null)}>
      <div className="legend"><span><i style={{ background: "var(--up)" }} />close ≥ open (hollow)</span><span><i style={{ background: "var(--down)" }} />close &lt; open (solid)</span>
        {markers.length > 0 && <><span>▲ buy fill</span><span>▼ sell fill</span></>}</div>
      <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Candlestick chart with trade fills">
        {ticks(lo, up).map((t) => <g key={t}><line x1={m.l} x2={W - m.r} y1={Y(t)} y2={Y(t)} stroke="var(--grid)" /><text className="axis" x={m.l - 6} y={Y(t) + 4} textAnchor="end">{compact(t)}</text></g>)}
        {[0, Math.floor(n / 2), n - 1].map((i, k) => <text key={k} className="axis" x={X(i)} y={height - 5} textAnchor={k === 0 ? "start" : k === 2 ? "end" : "middle"}>{ts[i].slice(0, 16)}</text>)}
        {ts.map((_, i) => {
          const upc = close[i] >= open[i], c = upc ? "var(--up)" : "var(--down)";
          const y1 = Y(Math.max(open[i], close[i])), y2 = Y(Math.min(open[i], close[i]));
          return <g key={i}><line x1={X(i)} x2={X(i)} y1={Y(high[i])} y2={Y(low[i])} stroke={c} strokeWidth={1} />
            <rect x={X(i) - Math.max(bw * 0.3, 1)} width={Math.max(bw * 0.6, 2)} y={y1} height={Math.max(y2 - y1, 1)} fill={upc ? "var(--surface)" : c} stroke={c} strokeWidth={1} /></g>;
        })}
        {lines.map((l) => <g key={l.label}><line x1={m.l} x2={W - m.r} y1={Y(l.price)} y2={Y(l.price)} stroke="var(--axis)" strokeDasharray="4 4" /><text className="axis" x={W - m.r} y={Y(l.price) - 4} textAnchor="end">{l.label}</text></g>)}
        {markers.map((k, j) => {
          const i = idx(k.ts);
          if (i < 0) return null;
          const y = Y(k.price), x = X(i), s = 7;
          return <g key={j}><path d={k.side > 0 ? `M${x},${y + 2}l${-s},${s * 1.6}h${2 * s}z` : `M${x},${y - 2}l${-s},${-s * 1.6}h${2 * s}z`} fill="var(--ink)" stroke="var(--surface)" strokeWidth={2} />
            <text className="axis" x={x} y={k.side > 0 ? y + s * 1.6 + 14 : y - s * 1.6 - 6} textAnchor="middle" style={{ fill: "var(--ink)" }}>{k.label}</text></g>;
        })}
        {hi != null && <line x1={X(hi)} x2={X(hi)} y1={m.t} y2={height - m.b} stroke="var(--axis)" />}
      </svg>
      {hi != null && <div className="tip" style={{ left: `${Math.min(Math.max((X(hi) / W) * 100, 12), 76)}%`, top: 30 }}>
        <b>{ts[hi].slice(0, 16)}</b>
        {[["open", open], ["high", high], ["low", low], ["close", close]].map(([k, a]) => <div key={k as string}><span>{k as string}</span><span>{(a as number[])[hi].toFixed(2)}</span></div>)}
      </div>}
    </div>
  );
}

/** Diverging heat-map for correlations in [-1, 1]: two hues around a neutral midpoint, value printed in every cell. */
export function Heatmap({ labels, matrix }: { labels: string[]; matrix: number[][] }) {
  const mix = (v: number) => {
    const a = Math.min(Math.abs(v), 1);
    return `color-mix(in oklab, ${v >= 0 ? "var(--s1)" : "var(--s2)"} ${Math.round(a * 100)}%, var(--div-mid))`;
  };
  if (!labels?.length) return <div className="empty">No data.</div>;
  return (
    <div style={{ overflow: "auto" }}>
      <div className="legend"><span><i style={{ background: "var(--s2)" }} />−1 (move opposite)</span><span><i style={{ background: "var(--div-mid)" }} />0</span><span><i style={{ background: "var(--s1)" }} />+1 (move together)</span></div>
      <table style={{ width: "auto", borderCollapse: "separate", borderSpacing: 2 }}>
        <thead><tr><th style={{ position: "static" }} />{labels.map((l) => <th key={l} style={{ position: "static", fontWeight: 500, fontSize: 11 }}>{l.replace("SYN", "")}</th>)}</tr></thead>
        <tbody>{matrix.map((row, i) => <tr key={i}><td style={{ border: 0, fontSize: 11 }} className="ink2">{labels[i].replace("SYN", "")}</td>
          {row.map((v, j) => <td key={j} title={`${labels[i]} × ${labels[j]}: ${v.toFixed(2)}`} style={{ background: mix(v), border: 0, borderRadius: 4, textAlign: "center", minWidth: 40, fontSize: 11,
            color: Math.abs(v) > 0.55 ? "#fff" : "var(--ink)" }}>{v.toFixed(2)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

/** Horizontal bars from a zero baseline; value label on every bar. */
export function Bars({ items, fmt = compact }: { items: { label: string; value: number | null }[]; fmt?: (v: number) => string }) {
  const vals = items.map((i) => i.value ?? 0);
  const max = Math.max(...vals.map(Math.abs), 1e-9), neg = vals.some((v) => v < 0);
  if (!items.length) return <div className="empty">No data.</div>;
  return (
    <div style={{ display: "grid", gap: 4 }}>
      {items.map((it) => {
        const v = it.value ?? 0, w = (Math.abs(v) / max) * (neg ? 50 : 100);
        return (
          <div key={it.label} style={{ display: "grid", gridTemplateColumns: "minmax(90px, 30%) 1fr 76px", gap: 8, alignItems: "center", fontSize: 12 }} title={`${it.label}: ${it.value == null ? "—" : fmt(v)}`}>
            <span className="ink2" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{it.label}</span>
            <span style={{ position: "relative", height: 12, borderLeft: neg ? "none" : "1px solid var(--axis)" }}>
              {neg && <span style={{ position: "absolute", left: "50%", top: -2, bottom: -2, borderLeft: "1px solid var(--axis)" }} />}
              <span style={{ position: "absolute", top: 0, height: 12, borderRadius: v >= 0 ? "0 4px 4px 0" : "4px 0 0 4px", background: "var(--s1)",
                left: neg ? (v >= 0 ? "50%" : `${50 - w}%`) : 0, width: `${w}%` }} />
            </span>
            <span style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{it.value == null ? "—" : fmt(v)}</span>
          </div>);
      })}
    </div>
  );
}

/** Donut of parts of a whole (e.g. cash vs each holding). Legend lists every slice with its value and share. */
export function Donut({ items, fmt = compact, centre, label }: { items: { label: string; value: number }[]; fmt?: (v: number) => string; centre?: React.ReactNode; label: string }) {
  const data = items.filter((x) => x.value > 0);
  const total = data.reduce((a, b) => a + b.value, 0);
  if (!total) return <div className="empty">Nothing to show yet.</div>;
  const R = 70, r = 46, C = 90;
  let a0 = -Math.PI / 2;
  const arcs = data.map((d, i) => {
    const a1 = a0 + (d.value / total) * Math.PI * 2 - (data.length > 1 ? 0.012 : 0);
    const big = a1 - a0 > Math.PI ? 1 : 0;
    const p = (rad: number, ang: number) => `${C + rad * Math.cos(ang)},${C + rad * Math.sin(ang)}`;
    const path = data.length === 1
      ? `M ${C - R},${C} A ${R},${R} 0 1,1 ${C + R},${C} A ${R},${R} 0 1,1 ${C - R},${C} M ${C - r},${C} A ${r},${r} 0 1,0 ${C + r},${C} A ${r},${r} 0 1,0 ${C - r},${C}`
      : `M ${p(R, a0)} A ${R},${R} 0 ${big},1 ${p(R, a1)} L ${p(r, a1)} A ${r},${r} 0 ${big},0 ${p(r, a0)} Z`;
    const out = { d, path, color: d.label === "Free cash" ? "var(--axis)" : seriesColor(i) };
    a0 = a0 + (d.value / total) * Math.PI * 2;
    return out;
  });
  return (
    <div style={{ display: "flex", gap: 18, alignItems: "center", flexWrap: "wrap" }}>
      <svg viewBox="0 0 180 180" width="180" height="180" role="img" aria-label={label} style={{ flex: "none" }}>
        {arcs.map((x) => <path key={x.d.label} d={x.path} fill={x.color} fillRule="evenodd"><title>{`${x.d.label}: ${fmt(x.d.value)} (${((100 * x.d.value) / total).toFixed(1)}%)`}</title></path>)}
        {centre && <foreignObject x="40" y="62" width="100" height="56"><div style={{ textAlign: "center", fontSize: 12, lineHeight: 1.25 }}>{centre}</div></foreignObject>}
      </svg>
      <ul className="list" style={{ flex: 1, minWidth: 160 }}>
        {arcs.map((x) => <li key={x.d.label} style={{ display: "flex", gap: 8, alignItems: "center", padding: "5px 0" }}>
          <i style={{ width: 10, height: 10, borderRadius: 3, background: x.color, display: "inline-block", flex: "none" }} />
          <span style={{ flex: 1 }}>{x.d.label}</span><span className="num">{fmt(x.d.value)}</span>
          <span className="dim num" style={{ width: 48, textAlign: "right" }}>{((100 * x.d.value) / total).toFixed(0)}%</span></li>)}
      </ul>
    </div>
  );
}

/** Ring showing a share (e.g. winning trades out of all closed trades). */
export function Ring({ part, total, label, color = "var(--good)", rest = "var(--crit)" }: { part: number; total: number; label: string; color?: string; rest?: string }) {
  const f = total ? part / total : 0, R = 42, L = 2 * Math.PI * R;
  return (
    <svg viewBox="0 0 110 110" width="110" height="110" role="img" aria-label={label}>
      <circle cx="55" cy="55" r={R} fill="none" stroke={total ? rest : "var(--border2)"} strokeWidth="12" opacity={total ? 0.85 : 1} />
      {total > 0 && <circle cx="55" cy="55" r={R} fill="none" stroke={color} strokeWidth="12" strokeDasharray={`${f * L} ${L}`} transform="rotate(-90 55 55)" strokeLinecap="butt" />}
      <text x="55" y="53" textAnchor="middle" style={{ font: "600 20px var(--cond)", fill: "var(--ink)" }}>{total ? `${Math.round(f * 100)}%` : "—"}</text>
      <text x="55" y="70" textAnchor="middle" style={{ fontSize: 10.5, fill: "var(--muted)" }}>{total ? "winners" : "no exits"}</text>
    </svg>
  );
}
