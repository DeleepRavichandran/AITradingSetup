"use client";
import { useState } from "react";
import { Heatmap, LineChart } from "@/components/charts";
import { runCols } from "@/components/common";
import { Banner, Card, DataTable, Err, Page } from "@/components/ui";
import { inr, num, pct, post, useApi } from "@/lib/api";

const ROWS: [string, string, (v: any) => string][] = [["sharpe", "Sharpe", num], ["sortino", "Sortino", num], ["calmar", "Calmar", num], ["total_return", "Total return", pct], ["cagr", "CAGR", pct],
  ["max_drawdown", "Max drawdown", pct], ["max_drawdown_days", "Drawdown duration (days)", (v) => num(v, 0)], ["volatility", "Volatility", pct], ["cvar_95", "CVaR 95 (daily)", (v) => pct(v, 2)],
  ["trades", "Trades", (v) => num(v, 0)], ["win_rate", "Win rate", pct], ["profit_factor", "Profit factor", num], ["expectancy", "Expectancy / trade", inr], ["avg_holding_days", "Avg holding (days)", (v) => num(v, 1)],
  ["total_costs", "Total costs", inr], ["turnover_annual", "Turnover ×/yr", (v) => num(v, 1)], ["exposure_time", "Time in market", pct], ["capacity_estimate", "Capacity estimate", inr], ["net_profit", "Net profit", inr]];

export default function Compare() {
  const runs = useApi<any[]>("/runs?limit=80");
  const [ids, setIds] = useState<string[]>([]);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const toggle = (id: string) => setIds(ids.includes(id) ? ids.filter((x) => x !== id) : ids.length < 8 ? [...ids, id] : ids);
  const go = async () => { setErr(null); try { setRes(await post("/compare", { run_ids: ids })); } catch (e: any) { setErr(e.message); } };
  const withFiles = (runs.data || []).filter((r) => r.path);
  const dates: string[] = res ? Array.from(new Set(Object.values(res.curves).flatMap((c: any) => c.ts))).sort() as string[] : [];
  const name = (r: any) => `${r.strategy} ${r.run_id.slice(0, 6)}`;
  return (
    <Page title="Strategy Comparison" sub="Side-by-side on identical data, period and cost assumptions. The table leads with risk-adjusted measures; net profit is deliberately last." right={<button className="primary" disabled={ids.length < 1} onClick={go}>Compare {ids.length} run{ids.length === 1 ? "" : "s"}</button>}>
      <Err e={err || runs.error} />
      <Card title="Choose up to 8 runs">
        <DataTable maxHeight={280} onRow={(r) => toggle(r.run_id)} selected={(r) => ids.includes(r.run_id)} cols={[{ key: "pick", label: "", f: (r) => (ids.includes(r.run_id) ? "☑" : "☐") }, ...runCols()]} rows={withFiles} empty="No stored runs yet." />
      </Card>
      {res && <>
        {res.warnings.map((w: string) => <div key={w} style={{ marginTop: 12 }}><Banner><b>Not like-for-like:</b> {w}</Banner></div>)}
        <div style={{ marginTop: 14 }}><Card title="Growth of 1 (all runs on one axis)">
          <LineChart x={dates} height={280} fmt={(v) => v.toFixed(2)} series={res.rows.filter((r: any) => res.curves[r.run_id]).map((r: any) => {
            const c = res.curves[r.run_id], map = new Map<string, number>(c.ts.map((t: string, i: number) => [t, c.value[i]]));
            return { name: name(r), values: dates.map((d) => map.get(d) ?? null) };
          })} />
        </Card></div>
        <div className="grid g21" style={{ marginTop: 14 }}>
          <Card title="Metrics">
            <DataTable maxHeight={600} cols={[{ key: "metric", f: (r: any) => r.label }, ...res.rows.map((r: any) => ({ key: r.run_id, label: name(r), r: true, f: (x: any) => x.f(r.metrics?.[x.key]) }))]}
              rows={ROWS.map(([key, label, f]) => ({ key, label, f }))} />
            <p className="dim" style={{ marginTop: 8 }}>{res.ranking_note}.</p>
          </Card>
          <Card title="Daily-return correlation">{res.correlation ? <Heatmap labels={res.rows.map(name)} matrix={res.correlation.matrix} /> : <div className="empty">Pick two or more runs.</div>}</Card>
        </div>
      </>}
    </Page>
  );
}
