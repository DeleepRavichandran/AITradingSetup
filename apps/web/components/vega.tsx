"use client";
/* VEGA building blocks. Every value comes from the API (/radar, /radar/{symbol}, /decisions, /integrity); nothing is
   illustrative. Decisions are shown as recorded at the time: no later outcome is used to rank or describe them. */
import Link from "next/link";
import React, { useEffect, useRef, useState } from "react";
import { inr, pct, useApi } from "@/lib/api";

export const STATE_TAG: Record<string, [string, string]> = {
  HOLD: ["info", "Holding"], TAKEN: ["ok", "Taken"], REFUSED: ["no", "Refused"], WATCH: ["", "Watching"],
};
export const Tag = ({ tone = "", children, testid }: { tone?: string; children: React.ReactNode; testid?: string }) =>
  <span className={`tag ${tone}`} data-testid={testid}><i />{children}</span>;

export const signedInr = (x: number | null | undefined, d = 0) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${inr(Math.abs(x), d)}`);
export const signedPct = (x: number | null | undefined, d = 2) => (x == null ? "—" : `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(d)}%`);
export const hhmm = (ts?: string | null) => (ts ? String(ts).slice(11, 16) : "—");

/** Radar rows. A row opens the decision dossier (click, Enter or Space). */
export function RadarTable({ rows, onOpen, limit, testid = "radar-table" }: { rows: any[]; onOpen: (s: string) => void; limit?: number; testid?: string }) {
  const shown = limit ? rows.slice(0, limit) : rows;
  if (!shown.length) return <p className="empty">No stock matches this filter.</p>;
  return (
    <div className="tw" style={{ ["--mh" as any]: "none" }}>
      <table data-testid={testid}>
        <thead><tr><th>Stock</th><th>Best setup today</th><th>Decision</th><th>Why</th><th className="r">Expected edge</th><th className="r">Today</th></tr></thead>
        <tbody>
          {shown.map((r) => {
            const [tone, label] = STATE_TAG[r.state] || ["", r.state];
            return (
              <tr key={r.symbol} className="click" tabIndex={0} aria-label={`Open the decision dossier for ${r.symbol}`} onClick={() => onOpen(r.symbol)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(r.symbol); } }}>
                <td><b>{r.symbol}</b><span className="st">{r.price != null ? `₹${r.price.toLocaleString("en-IN")}` : "no price yet"}{r.day_change != null ? ` · ${signedPct(r.day_change)}` : ""}</span></td>
                <td>{r.best_setup || <span className="dim">—</span>}<span className="st">{r.signals ? `${r.signals} buy signal${r.signals > 1 ? "s" : ""}${r.last_signal ? `, last ${hhmm(r.last_signal)}` : ""}` : "no buy signal"}</span></td>
                <td><Tag tone={tone}>{label}</Tag></td>
                <td style={{ whiteSpace: "normal", minWidth: 180 }}>{r.why}</td>
                <td className="r">{r.best_edge_bps == null ? "—" : <span className={r.best_edge_bps > 0 ? "up" : "down"}>{r.best_edge_bps > 0 ? "+" : ""}{r.best_edge_bps.toFixed(1)} bps</span>}</td>
                <td className="r">{r.position ? <span className={r.position.unrealised_inr >= 0 ? "up" : "down"}>{signedInr(r.position.unrealised_inr)}</span>
                  : r.sells ? <span className={r.realised_inr >= 0 ? "up" : "down"}>{signedInr(r.realised_inr)}</span> : <span className="dim">—</span>}</td>
              </tr>);
          })}
        </tbody>
      </table>
    </div>
  );
}

export const FILTERS: [string, string][] = [["all", "All"], ["HOLD", "Holding"], ["TAKEN", "Taken"], ["REFUSED", "Refused"], ["WATCH", "Watching"]];
export function Filter({ value, onChange, counts }: { value: string; onChange: (v: string) => void; counts?: Record<string, number> }) {
  return (
    <div className="vseg" role="group" aria-label="Filter by decision">
      {FILTERS.map(([k, l]) => <button key={k} aria-pressed={value === k} onClick={() => onChange(k)}>{l}{k !== "all" && counts ? ` ${counts[k] ?? 0}` : ""}</button>)}
    </div>
  );
}

/** Decision dossier: why the bot did (or did not) act on one stock, from the recorded decisions and the chart at the last candle. */
export function Dossier({ symbol, onClose }: { symbol: string | null; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const d = useApi<any>(symbol ? `/radar/${encodeURIComponent(symbol)}` : null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (symbol && !el.open) el.showModal?.();
    if (!symbol && el.open) el.close();
  }, [symbol]);
  const x = d.data, r = x?.row;
  const [tone, label] = STATE_TAG[r?.state] || ["", r?.state || "…"];
  return (
    <dialog ref={ref} className="dossier" onClose={onClose} aria-labelledby="dossier-title" data-testid="dossier">
      <header>
        <div><div className="dim" style={{ fontSize: 12 }}>Decision dossier · {x?.day || "latest session"}</div>
          <h2 id="dossier-title" style={{ fontSize: 19, margin: "2px 0 0" }}>{symbol} {r && <Tag tone={tone}>{label}</Tag>}</h2></div>
        <button className="x" onClick={() => ref.current?.close()} aria-label="Close the dossier">×</button>
      </header>
      <div className="body">
        {d.error && <p className="down">Could not load: {d.error}</p>}
        {!x && !d.error && <p className="dim">Loading the recorded decisions…</p>}
        {x && <>
          <p style={{ margin: 0 }}>{r.why}.</p>
          <div className="facts">
            <div className="fact"><span>Best setup today</span><b>{r.best_setup || "—"}</b></div>
            <div className="fact"><span>Expected edge (at decision)</span><b>{r.best_edge_bps == null ? "—" : `${r.best_edge_bps > 0 ? "+" : ""}${r.best_edge_bps.toFixed(1)} bps`}</b></div>
            <div className="fact"><span>Buy signals: accepted / refused</span><b>{r.accepted} / {r.refused}</b></div>
            <div className="fact"><span>Past round trips here</span><b>{x.history.round_trips} · {signedInr(x.history.net_inr)}</b></div>
          </div>
          <section aria-label="Gates">
            <h3 style={{ marginTop: 0 }}>Gates now</h3>
            <ul className="gates">{x.gates.map((g: any) => <li key={g.gate}><i style={{ background: g.ok ? "var(--good)" : "var(--warn)" }} />{g.gate}<span className="r">{g.detail}</span></li>)}</ul>
          </section>
          {x.chart?.read?.length > 0 && <section aria-label="Chart read">
            <h3 style={{ marginTop: 0 }}>Chart at the last candle {x.chart.as_of ? `(${String(x.chart.as_of).slice(11, 16)})` : ""}</h3>
            <ul className="list">{x.chart.read.map((t: string, i: number) => <li key={i} className="t" style={{ padding: "5px 0", fontSize: 13 }}>{t}</li>)}</ul>
          </section>}
          {Object.keys(x.signal_categories || {}).length > 0 && <section aria-label="Reasons">
            <h3 style={{ marginTop: 0 }}>Recorded reasons today</h3>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{Object.entries(x.signal_categories).map(([k, n]: any) =>
              <Tag key={k} tone={k === "accepted" ? "ok" : "no"}>{k}: {n}</Tag>)}</div>
          </section>}
          {x.signals.length > 0 && <div className="tw" style={{ ["--mh" as any]: "240px" }} tabIndex={0} aria-label="Signals today (scrollable)"><table>
            <thead><tr><th>Time</th><th>Setup</th><th>Decision</th><th className="r">Edge</th><th>Recorded reason</th></tr></thead>
            <tbody>{x.signals.slice(0, 40).map((s: any, i: number) => <tr key={i}><td>{s.time}</td><td>{s.setup} <span className="dim">{s.timeframe}</span></td>
              <td>{s.status === "accepted" ? <Tag tone="ok">accepted</Tag> : <Tag tone="no">{s.category}</Tag>}</td>
              <td className="r">{s.edge_bps == null ? "—" : s.edge_bps.toFixed(1)}</td><td style={{ maxWidth: 280 }} title={s.reason || ""}>{s.reason || "—"}</td></tr>)}</tbody>
          </table></div>}
          {x.orders.length > 0 && <div className="tw"><table>
            <thead><tr><th>Time</th><th>Side</th><th className="r">Qty</th><th className="r">Price</th><th className="r">Net</th><th>Setup</th></tr></thead>
            <tbody>{x.orders.map((o: any, i: number) => <tr key={i}><td>{o.time}</td><td>{o.side}</td><td className="r">{o.qty}</td><td className="r">{o.price ?? "—"}</td>
              <td className="r">{o.net_inr == null ? "—" : <span className={o.net_inr >= 0 ? "up" : "down"}>{signedInr(o.net_inr, 2)}</span>}</td><td>{o.setup}</td></tr>)}</tbody>
          </table></div>}
          <p className="dim" style={{ margin: 0, fontSize: 12 }}>{x.note}. Paper trading only: no order can be sent from this page. <Link href={`/charts?symbol=${symbol}`}>Open in Chart Analyst</Link></p>
        </>}
      </div>
    </dialog>
  );
}

const KIND: Record<string, [string, string]> = { BUY: ["var(--good)", "PAPER BUY"], SELL: ["var(--data)", "PAPER SELL"], REFUSED: ["var(--crit)", "REFUSED"], GATE: ["var(--warn)", "GATE"] };
export function Stream({ events, limit = 12 }: { events: any[]; limit?: number }) {
  if (!events?.length) return <p className="empty" style={{ padding: 16 }}>No decision recorded in this session yet.</p>;
  return (
    <ul className="events" data-testid="decision-stream" tabIndex={0} aria-label="Decision stream, newest first">
      {events.slice(0, limit).map((e, i) => {
        const [c, k] = KIND[e.kind] || ["var(--muted)", e.kind];
        return <li key={i}><span className="t">{hhmm(e.ts)}</span><span className="d" style={{ color: c }} />
          <div><b><span className="k" style={{ color: c }}>{k}</span>{e.symbol || "Portfolio"}</b><p>{e.text}</p></div></li>;
      })}
    </ul>
  );
}

/** Risk and execution usage against the account's own (operator-approved) rules. */
export function Usage({ usage }: { usage?: any[] }) {
  if (!usage?.length) return <p className="dim" style={{ margin: 0 }}>No shared-capital rules on this account.</p>;
  return (
    <div className="usage" data-testid="risk-usage">
      {usage.map((u) => {
        const share = u.limit ? Math.min(1, u.used / u.limit) : 0;
        const fmt = (v: number) => (u.unit === "pct" ? pct(v, 1) : String(v));
        return <div key={u.name}><div className="rl">{u.name}{u.detail ? ` (${u.detail})` : ""} <b>{fmt(u.used)} / {fmt(u.limit)}</b></div>
          <div className={`track ${share >= 1 ? "crit" : share >= 0.75 ? "warn" : ""}`} role="meter" aria-label={u.name} aria-valuemin={0} aria-valuemax={u.limit} aria-valuenow={u.used}>
            <i style={{ width: `${share * 100}%` }} /></div></div>;
      })}
    </div>
  );
}

/** Evidence status (validation_protocol.STATUSES) -> tag tone. "Validated" is never used for a study result. */
export const statusTone = (v?: string) => (v === "holdout-supported finding" || v === "forward-supported finding" || v === "promotion-eligible methodology" ? "ok"
  : v === "rejected" ? "no" : v ? "wait" : "");
