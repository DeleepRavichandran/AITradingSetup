"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { LineChart } from "@/components/charts";
import { ResearchTrail, readCtx, useCtx } from "@/components/trail";
import { Badge, Card, DataTable, Err, KV, Page, Stat, Synthetic } from "@/components/ui";
import { num, pct, post, useApi, useJob } from "@/lib/api";

export default function WalkForward() {
  const ctx = useCtx();
  const strategies = useApi<any>("/strategies");
  const reports = useApi<any[]>("/walkforward");
  const [f, setF] = useState({ strategy: "sma_cross", grid: "", train_days: 250, test_days: 60, mode: "rolling", max_points: 12 });
  const [jobId, setJobId] = useState<string | null>(null);
  useEffect(() => { const c = readCtx(); if (c.strategy) setF((x) => ({ ...x, strategy: c.strategy as string })); }, []);
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, running } = useJob(jobId);
  const lib = strategies.data?.library || [], gen = strategies.data?.generated || [];
  const go = async () => {
    setErr(null);
    try {
      const r = await post("/walkforward", { ...f, grid: f.grid.trim() ? JSON.parse(f.grid) : {} });
      setJobId(r.id); setSel(null);
    } catch (e: any) { setErr(e.message); }
  };
  if (job?.status === "done" && !sel && jobId) { setSel(job.result.artifact_id); setJobId(null); reports.reload(); }
  const w = (reports.data || []).find((r) => r.id === sel) || (job?.status === "done" ? job.result : null) || reports.data?.[0];
  const cur = lib.find((s: any) => s.name === f.strategy);
  return (
    <Page title="Walk-Forward Testing" sub="For each fold the parameters are chosen using the training window only, then frozen and applied to the next, unseen window. Stitching those unseen windows together gives an honest out-of-sample curve.">
      <ResearchTrail here="/walkforward" ctx={{ strategy: f.strategy, dataset: ctx.dataset }} />
      <Card title="Configure">
        <div className="row">
          <label>Strategy<select value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value, grid: "" })}>
            <optgroup label="Library">{lib.filter((x: any) => Object.keys(x.param_grid || {}).length).map((x: any) => <option key={x.name}>{x.name}</option>)}</optgroup>
            <optgroup label="Generated">{gen.map((x: any) => <option key={x.code_id} value={`generated:${x.code_id}`}>{x.hypothesis_id}</option>)}</optgroup></select></label>
          <label>Mode<select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}><option>rolling</option><option>expanding</option></select></label>
          <label>Train sessions<input type="number" value={f.train_days} onChange={(e) => setF({ ...f, train_days: Number(e.target.value) })} style={{ width: 100 }} /></label>
          <label>Test sessions<input type="number" value={f.test_days} onChange={(e) => setF({ ...f, test_days: Number(e.target.value) })} style={{ width: 100 }} /></label>
          <label>Max grid points<input type="number" value={f.max_points} onChange={(e) => setF({ ...f, max_points: Number(e.target.value) })} style={{ width: 100 }} /></label>
          <label style={{ flex: 1, minWidth: 220 }}>Grid (JSON, blank = strategy default)<input className="mono" value={f.grid} placeholder={cur ? JSON.stringify(cur.param_grid) : ""} onChange={(e) => setF({ ...f, grid: e.target.value })} /></label>
          <button className="primary" onClick={go} disabled={running}>{running ? "Running…" : "Run walk-forward"}</button>
        </div>
      </Card>
      <Err e={err || job?.error} />
      {w && <>
        <div className="top" style={{ marginTop: 16 }}><h1>{w.strategy} · {w.mode}</h1><span className="dim mono">{w.id || w.artifact_id}</span></div>
        <Synthetic on={w.synthetic} />
        <div className="stats">
          <Stat k="Out-of-sample Sharpe" v={num(w.oos.sharpe)} h={`${w.oos.sessions} unseen sessions`} /><Stat k="Out-of-sample return" v={pct(w.oos.total_return)} /><Stat k="Out-of-sample max DD" v={pct(w.oos.max_drawdown)} />
          <Stat k="Profitable folds" v={`${w.oos.positive_folds}/${w.oos.folds}`} /><Stat k="In-sample Sharpe (mean)" v={num(w.mean_in_sample_sharpe)} h="what optimisation promised" />
          <Stat k="Efficiency" v={num(w.walk_forward_efficiency)} h="out-of-sample ÷ in-sample" /><Stat k="Parameter sets chosen" v={w.param_drift} h={`of ${w.grid_points} candidates`} /><Stat k="Backtests run" v={w.backtests_run} />
        </div>
        <div className="grid g21" style={{ marginTop: 14 }}>
          <Card title="Stitched out-of-sample equity (start = 1)"><LineChart x={w.curve.dates} series={[{ name: "out-of-sample", values: w.curve.values }]} fmt={(v) => v.toFixed(3)} /></Card>
          <Card title="Audit trail"><KV items={[["dataset", w.dataset_id], ["dataset hash", <span key="h" className="mono">{String(w.dataset_hash).slice(0, 24)}…</span>], ["window", w.window.join(" → ")], ["note", w.window_note],
            ["grid", <span key="g" className="mono">{JSON.stringify(w.grid)}</span>], ["train / test", `${w.train_days} / ${w.test_days} sessions`]]} /></Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Folds">
          <DataTable maxHeight={360} cols={[{ key: "fold", r: true }, { key: "train", f: (r) => r.train.join(" → ") }, { key: "test", f: (r) => r.test.join(" → ") }, { key: "chosen_params", label: "chosen on train", f: (r) => <span className="mono">{JSON.stringify(r.chosen_params)}</span> },
            { key: "in_sample_sharpe", label: "train Sharpe", r: true, f: (r) => num(r.in_sample_sharpe) }, { key: "oos_sharpe", label: "test Sharpe", r: true, f: (r) => num(r.oos_sharpe) },
            { key: "oos_return", label: "test return", r: true, f: (r) => <span className={r.oos_return > 0 ? "up" : r.oos_return < 0 ? "down" : ""}>{pct(r.oos_return)}</span> },
            { key: "run_id", label: "run", f: (r) => (r.run_id ? <Link className="mono" href={`/backtest?run=${r.run_id}`}>{r.run_id}</Link> : <Badge s={r.skipped || "skipped"} tone="warn" />) }]} rows={w.folds} />
        </Card></div>
      </>}
      <div style={{ marginTop: 14 }}><Card title="Saved reports">
        <DataTable maxHeight={240} onRow={(r) => setSel(r.id)} selected={(r) => r.id === (w?.id || w?.artifact_id)} cols={[{ key: "created_at", label: "created", f: (r) => r.created_at.replace("T", " ") }, { key: "strategy" }, { key: "mode" },
          { key: "oos", label: "OOS Sharpe", r: true, f: (r) => num(r.oos?.sharpe) }, { key: "eff", label: "efficiency", r: true, f: (r) => num(r.walk_forward_efficiency) }, { key: "folds", r: true, f: (r) => r.folds?.length }]} rows={reports.data} empty="No walk-forward reports yet." />
      </Card></div>
    </Page>
  );
}
