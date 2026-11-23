"use client";
import Link from "next/link";
import { useState } from "react";
import { Bars, Heatmap } from "@/components/charts";
import { MetricTiles } from "@/components/common";
import { Badge, Card, DataTable, Err, KV, Page, Stat, Synthetic } from "@/components/ui";
import { AccountKinds } from "@/components/kinds";
import { inr, num, pct, post, useApi } from "@/lib/api";

export default function Portfolio() {
  const strategies = useApi<any>("/strategies");
  const [picked, setPicked] = useState<string[]>(["sma_cross", "rsi_reversion", "donchian_breakout"]);
  const [method, setMethod] = useState("risk_parity");
  const [capital, setCapital] = useState(3000000);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const lib = (strategies.data?.library || []).filter((s: any) => s.intervals.includes("1d") && s.family !== "baseline");
  const go = async () => {
    setBusy(true); setErr(null);
    try { setRes(await post("/portfolio/simulate", { strategies: picked.map((name) => ({ name })), method, capital })); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  return (
    <Page title="Portfolio Simulation" sub="Several strategies trading at once on ONE account and one chronological event stream. Each gets its own capital book, so two strategies can never spend the same rupee. Weights are estimated on development data and evaluated on the validation window.">
      <AccountKinds here="portfolio" />
      <Card title="Configure">
        <div className="row" style={{ marginBottom: 10 }}>
          <label>Allocation method<select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="equal">equal weight</option><option value="risk_parity">risk parity (inverse volatility)</option><option value="min_variance">minimum variance (long-only, capped)</option><option value="vol_target">volatility target 10%</option></select></label>
          <label>Total capital (₹)<input type="number" value={capital} onChange={(e) => setCapital(Number(e.target.value))} style={{ width: 140 }} /></label>
          <button className="primary" onClick={go} disabled={busy || picked.length < 1}>{busy ? "Simulating…" : `Simulate ${picked.length} strategies`}</button>
        </div>
        <div className="row">{lib.map((s: any) => (
          <label key={s.name} style={{ display: "flex", gap: 5, alignItems: "center" }}><input type="checkbox" checked={picked.includes(s.name)} onChange={() => setPicked(picked.includes(s.name) ? picked.filter((x) => x !== s.name) : picked.length < 8 ? [...picked, s.name] : picked)} />{s.name}</label>))}</div>
      </Card>
      <Err e={err} />
      {res && <>
        <div className="top" style={{ marginTop: 16 }}><h1>Result</h1><Link className="mono" href={`/backtest?run=${res.run_id}`}>{res.run_id}</Link></div>
        <Synthetic on={res.synthetic} />
        <div className="stats">
          <Stat k="Validation Sharpe" v={num(res.validation_window.sharpe)} h={res.evaluated_on.join(" → ")} /><Stat k="Validation return" v={pct(res.validation_window.total_return)} /><Stat k="Validation max DD" v={pct(res.validation_window.max_drawdown)} />
          <Stat k="Avg pair correlation" v={num(res.correlation_validation.average)} h="validation window" /><Stat k="Peak margin used" v={inr(res.capital_check.margin_used_peak)} /><Stat k="Orders refused for capital" v={res.rejected_for_capital} h="a book ran out of its own cash" />
        </div>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="Capital weights"><Bars items={[...Object.entries(res.weights).map(([label, value]) => ({ label, value: value as number })), { label: "cash (unallocated)", value: res.cash_weight }]} fmt={(v) => pct(v)} />
            <p className="dim" style={{ marginTop: 8 }}>Estimated on {res.weights_estimated_on.join(" → ")} only.</p></Card>
          <Card title="Shared-ledger check"><KV items={[["sum of book cash", inr(res.capital_check.sum_of_book_cash, 2)], ["account cash", inr(res.capital_check.account_cash, 2)],
            ["ledger reconciliation", <Badge key="r" s={res.capital_check.reconciliation_ok ? "pass" : "fail"} />], ["peak gross exposure", inr(res.capital_check.gross_exposure_peak)]]} />
            <h3>Sector exposure at the end</h3><Bars items={Object.entries(res.exposure.sector_exposure).map(([label, value]) => ({ label, value: value as number }))} fmt={(v) => inr(v)} /></Card>
          <Card title="Strategy correlation · development"><Heatmap labels={res.correlation_dev.labels} matrix={res.correlation_dev.matrix} /></Card>
          <Card title="Strategy correlation · validation"><Heatmap labels={res.correlation_validation.labels} matrix={res.correlation_validation.matrix} /></Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Per strategy (full run)">
          <DataTable<any> cols={[{ key: "book" }, { key: "sharpe", r: true, f: (r) => num(r.m.sharpe) }, { key: "ret", label: "return", r: true, f: (r) => pct(r.m.total_return) }, { key: "dd", label: "max DD", r: true, f: (r) => pct(r.m.max_drawdown) },
            { key: "trades", r: true, f: (r) => r.m.trades }, { key: "costs", r: true, f: (r) => inr(r.m.total_costs) }, { key: "cap", label: "capital", r: true, f: (r) => inr(r.m.initial_capital) }]} rows={Object.entries(res.per_book).map(([book, m]) => ({ book, m }))} />
        </Card></div>
        <div style={{ marginTop: 14 }}><Card title="Symbols held by more than one strategy"><DataTable maxHeight={200} cols={[{ key: "symbol" }, { key: "books", f: (r) => Object.entries(r.books).map(([b, v]: any) => `${b}: ${inr(v)}`).join(" · ") }, { key: "total_value", label: "total", r: true, f: (r) => inr(r.total_value) }]} rows={res.overlap} empty="No overlapping positions at the end of the run." /></Card></div>
        <div style={{ marginTop: 14 }}><Card title="Whole portfolio, full run"><MetricTiles m={res.metrics_full_run} /></Card></div>
      </>}
    </Page>
  );
}
