"use client";
import Link from "next/link";
import React from "react";
import { day, inr, num, pct } from "@/lib/api";
import { Badge, Col, Stat } from "./ui";

export const METRIC_HELP = "Sharpe uses daily returns, 252 sessions, zero risk-free rate. All figures are net of modelled costs.";

export function MetricTiles({ m }: { m: any }) {
  if (!m) return null;
  return (
    <div className="stats">
      <Stat k="Net profit" v={inr(m.net_profit)} h={`${pct(m.total_return)} on ${inr(m.initial_capital)}`} tone={m.net_profit > 0 ? "up" : m.net_profit < 0 ? "down" : undefined} />
      <Stat k="Sharpe" v={num(m.sharpe)} h={`Sortino ${num(m.sortino)}`} />
      <Stat k="Max drawdown" v={pct(m.max_drawdown)} h={`${m.max_drawdown_days ?? "—"} days under water`} />
      <Stat k="CAGR" v={m.cagr == null ? "n/a" : pct(m.cagr)} h={m.cagr == null ? "sample under 6 months" : `Calmar ${num(m.calmar)}`} />
      <Stat k="Trades" v={num(m.trades, 0)} h={`win rate ${pct(m.win_rate)}`} />
      <Stat k="Profit factor" v={num(m.profit_factor)} h={`expectancy ${inr(m.expectancy)}`} />
      <Stat k="Total costs" v={inr(m.total_costs)} h={m.costs_pct_of_gross_pnl == null ? "" : `${pct(m.costs_pct_of_gross_pnl)} of gross P&L`} />
      <Stat k="Exposure" v={pct(m.exposure_time)} h={`turnover ${num(m.turnover_annual, 1)}×/yr`} />
      <Stat k="Tail (CVaR 95)" v={pct(m.cvar_95, 2)} h={`worst day ${pct(m.worst_day, 2)}`} />
      <Stat k="Avg holding" v={m.avg_holding_days == null ? "—" : `${num(m.avg_holding_days, 1)} d`} h={m.capacity_estimate ? `capacity ≈ ${inr(m.capacity_estimate)}` : ""} />
    </div>
  );
}

export const runCols = (): Col[] => [
  { key: "run_id", label: "run", f: (r) => <Link href={`/backtest?run=${r.run_id}`} className="mono">{r.run_id}</Link> },
  { key: "strategy" }, { key: "kind" }, { key: "partition" },
  { key: "sharpe", r: true, f: (r) => num(r.metrics?.sharpe), sort: (r) => r.metrics?.sharpe },
  { key: "return", r: true, f: (r) => pct(r.metrics?.total_return), sort: (r) => r.metrics?.total_return },
  { key: "max_dd", label: "max DD", r: true, f: (r) => pct(r.metrics?.max_drawdown), sort: (r) => r.metrics?.max_drawdown },
  { key: "trades", r: true, f: (r) => num(r.metrics?.trades, 0), sort: (r) => r.metrics?.trades },
  { key: "period", f: (r) => `${day(r.period_start)} → ${day(r.period_end)}` },
  { key: "data", f: (r) => (r.synthetic ? <Badge s="synthetic" tone="warn" /> : <Badge s="real" tone="good" />) },
  { key: "status", f: (r) => <Badge s={r.status} /> },
  { key: "created_at", label: "created", f: (r) => String(r.created_at).replace("T", " ") },
];
