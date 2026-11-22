"use client";
import React, { useMemo, useState } from "react";

export const Card = ({ title, children, right }: { title?: React.ReactNode; children: React.ReactNode; right?: React.ReactNode }) => (
  <section className="card">
    {(title || right) && <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}><h2>{title}</h2>{right}</div>}
    {children}
  </section>
);

export const Stat = ({ k, v, h, tone }: { k: string; v: React.ReactNode; h?: React.ReactNode; tone?: "up" | "down" }) => (
  <div className="stat"><div className="k">{k}</div><div className={`v ${tone || ""}`}>{v}</div>{h && <div className="h">{h}</div>}</div>
);

const TONE: Record<string, string> = {
  ok: "good", pass: "good", done: "good", IMPLEMENTED: "good", promoted_to_paper_candidate: "good", paper_candidate: "good", FILLED: "good", survived: "good",
  fail: "crit", failed: "crit", error: "crit", rejected: "crit", REJECTED: "crit", BLOCKED: "crit", rejected_on_sealed: "crit", "NOT IMPLEMENTED": "crit", DISABLED: "crit", "NOT CONFIGURED": "crit",
  warn: "warn", UNVERIFIED: "warn", running: "info", advance_to_sealed: "info", PARTIAL: "warn", OPEN: "info", PAPER_ONLY: "warn", info: "info", proposed: "info",
};
/** Status is always icon + text, never colour alone. */
export const Badge = ({ s, tone }: { s: any; tone?: string }) => {
  const t = String(s ?? "—");
  const key = Object.keys(TONE).find((k) => t === k || t.startsWith(k));
  return <span className={`badge ${tone || (key ? TONE[key] : "")}`}><i />{t.replaceAll("_", " ")}</span>;
};

export const Banner = ({ children, tone = "warn" }: { children: React.ReactNode; tone?: "warn" | "crit" | "info" | "good" }) => (
  <div className={`banner ${tone === "warn" ? "" : tone}`}>{children}</div>
);
export const Synthetic = ({ on }: { on?: boolean | null }) =>
  on ? <Banner><b>Synthetic data.</b> These numbers come from a generated fixture universe, not from NSE. They demonstrate that the machinery works and say nothing about real markets.</Banner> : null;
export const Err = ({ e }: { e?: string | null }) => (e ? <Banner tone="crit"><b>Error:</b> {e}</Banner> : null);
export const Loading = ({ on, what = "Loading" }: { on?: boolean; what?: string }) => (on ? <div className="empty">{what}…</div> : null);

export type Col<T = any> = { key: string; label?: string; r?: boolean; f?: (row: T, i: number) => React.ReactNode; sort?: (row: T) => any };

export function DataTable<T = any>({ cols, rows, onRow, selected, maxHeight = 460, empty = "Nothing to show yet.", wrap, limit = 500 }: {
  cols: Col<T>[]; rows: T[] | null | undefined; onRow?: (row: T, i: number) => void; selected?: (row: T, i: number) => boolean;
  maxHeight?: number; empty?: string; wrap?: boolean; limit?: number;
}) {
  const [sort, setSort] = useState<{ k: string; d: 1 | -1 } | null>(null);
  const data = useMemo(() => {
    const base = (rows || []).map((r, i) => ({ r, i }));
    if (!sort) return base;
    const c = cols.find((x) => x.key === sort.k);
    const val = (x: any) => (c?.sort ? c.sort(x) : x[sort.k]);
    return [...base].sort((a, b) => {
      const x = val(a.r), y = val(b.r);
      return (x == null ? 1 : y == null ? -1 : x > y ? 1 : x < y ? -1 : 0) * sort.d;
    });
  }, [rows, sort, cols]);
  if (!rows || rows.length === 0) return <div className="empty">{empty}</div>;
  return (
    <>
      <div className="tw" style={{ ["--mh" as any]: `${maxHeight}px` }}>
        <table className={wrap ? "wrap" : ""}>
          <thead><tr>{cols.map((c) => (
            <th key={c.key} className={c.r ? "r" : ""} onClick={() => setSort(sort?.k === c.key ? { k: c.key, d: sort.d === 1 ? -1 : 1 } : { k: c.key, d: 1 })}>
              {c.label ?? c.key}{sort?.k === c.key ? (sort.d === 1 ? " ▲" : " ▼") : ""}
            </th>))}</tr></thead>
          <tbody>{data.slice(0, limit).map(({ r, i }) => (
            <tr key={i} className={`${onRow ? "click" : ""} ${selected?.(r, i) ? "sel" : ""}`} onClick={onRow ? () => onRow(r, i) : undefined}>
              {cols.map((c) => <td key={c.key} className={c.r ? "r" : ""}>{c.f ? c.f(r, i) : String((r as any)[c.key] ?? "—")}</td>)}
            </tr>))}</tbody>
        </table>
      </div>
      {data.length > limit && <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>showing {limit} of {data.length} rows</div>}
    </>
  );
}

export const KV = ({ items }: { items: [string, React.ReactNode][] }) => (
  <dl className="kv">{items.map(([k, v]) => <React.Fragment key={k}><dt>{k}</dt><dd>{v}</dd></React.Fragment>)}</dl>
);
export const Signed = ({ v, f }: { v: number | null | undefined; f: (x: any) => string }) =>
  v == null ? <span className="dim">—</span> : <span className={v > 0 ? "up" : v < 0 ? "down" : ""}>{v > 0 ? "▲ " : v < 0 ? "▼ " : ""}{f(Math.abs(v))}</span>;
export const Page = ({ title, sub, children, right }: { title: string; sub?: React.ReactNode; children: React.ReactNode; right?: React.ReactNode }) => (
  <>
    <div className="top"><h1>{title}</h1><span className="sp" />{right}</div>
    {sub && <p className="sub">{sub}</p>}
    {children}
  </>
);

/** Market-data provenance from the backend (nseql.screening.provenance). Never inferred in the browser. */
export const DataSource = ({ d, compact }: { d?: any; compact?: boolean }) => {
  if (!d) return null;
  const tone = d.stale ? "crit" : d.verified ? "good" : d.live ? "warn" : d.kind === "none" ? "warn" : "info";
  const bits = [d.delay_seconds != null ? `delay ≈ ${d.delay_seconds >= 120 ? `${Math.round(d.delay_seconds / 60)} min` : `${d.delay_seconds} s`}` : null,
    d.freshness_seconds != null ? `last bar ${Math.round(d.freshness_seconds)} s ago` : null,
    d.market_open === false && d.live ? "market closed" : null].filter(Boolean);
  if (compact) return <Badge s={d.label} tone={tone} />;
  return (
    <Banner tone={tone === "good" ? "good" : tone === "crit" ? "crit" : tone === "warn" ? "warn" : "info"}>
      <b>Data: {d.label}.</b>{bits.length ? ` ${bits.join(" · ")}.` : ""}{d.detail ? ` ${d.detail}.` : ""}{d.synthetic ? " Synthetic data says nothing about real markets." : ""}
    </Banner>
  );
};

/* ===================================================================== V6 design-system components */

/** Kind of data behind a number, from the backend provenance record. */
export const provKind = (d?: any): { q: string; text: string } => {
  if (!d) return { q: "", text: "source unknown" };
  if (d.synthetic || d.kind === "synthetic_replay") return { q: "synthetic", text: "synthetic data" };
  if (d.kind === "replay") return { q: "replay", text: "replayed history" };
  if (d.kind === "end_of_day") return { q: "replay", text: "end-of-day (delayed)" };
  if (d.kind === "live_verified") return { q: "live", text: "live, verified" };
  if (d.kind === "live_unverified") return { q: "unverified", text: "live, adapter unverified" };
  if (d.kind === "none") return { q: "", text: "no market data" };
  return { q: "", text: String(d.label || d.kind || "unknown") };
};

/** Evidence footer: source · period · updated · data quality. Every number on a panel inherits it. */
export const Prov = ({ source, period, updated, data, note }: { source: React.ReactNode; period?: any; updated?: string | null; data?: any; note?: React.ReactNode }) => {
  const k = provKind(data);
  const per = Array.isArray(period) ? period.filter(Boolean).map((x: string) => String(x).replace("T", " ").slice(0, 16)).join(" → ") : period;
  return (
    <div className="prov" data-testid="prov">
      <span><b>Source</b> {source}</span>
      {per && <span><b>Period</b> {per}</span>}
      {updated && <span><b>Updated</b> {String(updated).replace("T", " ").slice(0, 19)}</span>}
      {data !== undefined && <span className={`q ${k.q}`}><i />{k.text}</span>}
      {note && <span>{note}</span>}
    </div>
  );
};

export const Panel = ({ title, hint, right, children, prov, id, className = "" }: {
  title: React.ReactNode; hint?: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; prov?: React.ReactNode; id?: string; className?: string;
}) => {
  const hid = id ? `${id}-h` : undefined;
  return (
    <section className={`panel ${className}`} aria-labelledby={hid} id={id}>
      <div className="ph"><h2 id={hid}>{title}</h2>{hint && <span className="hint">{hint}</span>}<span style={{ flex: 1 }} />{right}</div>
      <div className="pb">{children}</div>
      {prov}
    </section>
  );
};

export const Metric = ({ k, v, h, tone, sm, testid }: { k: React.ReactNode; v: React.ReactNode; h?: React.ReactNode; tone?: "up" | "down"; sm?: boolean; testid?: string }) => (
  <div className={`metric ${sm ? "sm" : ""}`} data-testid={testid}><div className="k">{k}</div><div className={`v ${tone || ""}`}>{v ?? "—"}</div>{h && <div className="h">{h}</div>}</div>
);

const STATUS_TONE: Record<string, string> = { ready: "good", ok: "good", running: "good", open: "good", idle: "info", disabled: "info", "not configured": "info",
  unverified: "warn", degraded: "warn", blocked: "crit", engaged: "crit", unavailable: "crit", error: "crit" };
export const toneOf = (s: string) => STATUS_TONE[String(s).toLowerCase()] || (String(s).startsWith("closed") ? "info" : "");

export const StatusGrid = ({ items }: { items: { name: string; status: string; detail?: React.ReactNode }[] }) => (
  <div className="status-grid" role="list">
    {items.map((m) => (
      <div key={m.name} className={`status-cell ${toneOf(m.status)}`} role="listitem">
        <div className="n"><i aria-hidden="true" />{m.name}</div>
        <div className="s">{m.status}</div>
        {m.detail && <div className="d">{m.detail}</div>}
      </div>))}
  </div>
);

export const Unavailable = ({ title, children }: { title: React.ReactNode; children?: React.ReactNode }) => (
  <div className="unavail" role="note"><b>{title}</b>{children}</div>
);

export const Skeleton = ({ lines = 3 }: { lines?: number }) => (
  <div aria-busy="true" aria-label="Loading" style={{ display: "grid", gap: 8 }}>{Array.from({ length: lines }, (_, i) => <div key={i} className="skel" style={{ width: `${90 - i * 15}%` }} />)}</div>
);

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: [T, string][]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([k, l]) => <button key={k || "all"} type="button" aria-pressed={value === k} onClick={() => onChange(k)}>{l}</button>)}
    </div>
  );
}

export const Funnel = ({ steps, fmt = (n: number) => n.toLocaleString("en-IN") }: { steps: { stage: string; n: number }[]; fmt?: (n: number) => string }) => {
  const max = Math.max(1, ...steps.map((s) => s.n));
  return (
    <div className="funnel" role="list">
      {steps.map((s) => (
        <div className="fr" role="listitem" key={s.stage}>
          <span className="ink2">{s.stage}</span><div className="bar" style={{ width: `${(100 * s.n) / max}%` }} aria-hidden="true" /><span className="r num">{fmt(s.n)}</span>
        </div>))}
    </div>
  );
};

/** Minimal sparkline for short series (latency, equity). */
export const Spark = ({ values, label }: { values: (number | null)[]; label: string }) => {
  const v = values.filter((x): x is number => x != null && Number.isFinite(x));
  if (v.length < 2) return <span className="dim">not enough points</span>;
  const lo = Math.min(...v), hi = Math.max(...v), W = 300, H = 44;
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * W},${H - 3 - ((x - lo) / (hi - lo || 1)) * (H - 6)}`).join(" ");
  return <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={label}><polyline points={pts} fill="none" stroke="var(--data)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" /></svg>;
};

/** Windowed table: renders only visible rows, so thousands of rows stay responsive. Server pages feed it. */
export function VirtualTable<T = any>({ cols, rows, rowHeight = 34, height = 480, onRow, selected, empty = "Nothing to show." }: {
  cols: Col<T>[]; rows: T[]; rowHeight?: number; height?: number; onRow?: (row: T, i: number) => void; selected?: (row: T) => boolean; empty?: string;
}) {
  const [top, setTop] = useState(0);
  if (!rows.length) return <div className="empty">{empty}</div>;
  const first = Math.max(0, Math.floor(top / rowHeight) - 5), last = Math.min(rows.length, Math.ceil((top + height) / rowHeight) + 5);
  return (
    <div className="tw" style={{ ["--mh" as any]: `${height}px` }} onScroll={(e) => setTop((e.target as HTMLDivElement).scrollTop)} data-testid="vtable">
      <table>
        <thead><tr>{cols.map((c) => <th key={c.key} className={c.r ? "r" : ""}>{c.label ?? c.key}</th>)}</tr></thead>
        <tbody>
          {first > 0 && <tr aria-hidden="true" style={{ height: first * rowHeight }}><td colSpan={cols.length} style={{ padding: 0, border: 0 }} /></tr>}
          {rows.slice(first, last).map((r, k) => {
            const i = first + k;
            return (
              <tr key={i} style={{ height: rowHeight }} className={`${onRow ? "click" : ""} ${selected?.(r) ? "sel" : ""}`} onClick={onRow ? () => onRow(r, i) : undefined}
                tabIndex={onRow ? 0 : undefined} onKeyDown={onRow ? (e) => { if (e.key === "Enter") onRow(r, i); } : undefined}>
                {cols.map((c) => <td key={c.key} className={c.r ? "r" : ""}>{c.f ? c.f(r, i) : String((r as any)[c.key] ?? "—")}</td>)}
              </tr>);
          })}
          {last < rows.length && <tr aria-hidden="true" style={{ height: (rows.length - last) * rowHeight }}><td colSpan={cols.length} style={{ padding: 0, border: 0 }} /></tr>}
        </tbody>
      </table>
    </div>
  );
}
