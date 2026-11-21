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
