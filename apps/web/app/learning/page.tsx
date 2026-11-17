"use client";
import { useState } from "react";
import { LineChart } from "@/components/charts";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page, Stat, Synthetic } from "@/components/ui";
import { num, pct, post, useApi, useJob } from "@/lib/api";

const fmtKpi = (k: string, v: any) => (v == null ? "—" : ["oos_lift"].includes(k) ? `${(v * 100).toFixed(3)}% / window` : ["research_efficiency", "regime_robustness"].includes(k) ? pct(v, 2) : num(v, 3));
const statusTone = (s: string) => (s === "met" ? "good" : s.startsWith("not met") ? "crit" : s === "reported" ? "info" : "warn");

function CompTable({ comps, note }: { comps: Record<string, any> | null | undefined; note?: string }) {
  if (!comps) return <div className="empty">No comparisons.</div>;
  const rows = Object.entries(comps).map(([k, v]: any) => ({ k, f: v.forward_return, q: v.screen_quality }));
  return (<>
    <DataTable maxHeight={320} cols={[{ key: "k", label: "comparison (memory arm vs memory-free counterpart)", f: (r) => r.k.replaceAll("_", " ") },
      { key: "d", label: "forward diff / window", r: true, f: (r) => <span className={r.f.ci90[0] > 0 ? "up" : r.f.ci90[1] < 0 ? "down" : ""}>{(r.f.mean_diff * 100).toFixed(3)}%</span>, sort: (r) => r.f.mean_diff },
      { key: "ci", label: "90% CI", f: (r) => `${(r.f.ci90[0] * 100).toFixed(3)}% … ${(r.f.ci90[1] * 100).toFixed(3)}%` },
      { key: "p", label: "p", r: true, f: (r) => num(r.f.p_two_sided, 3) }, { key: "ph", label: "p (Holm)", r: true, f: (r) => num(r.f.p_holm, 3) },
      { key: "w", label: "win rate", r: true, f: (r) => pct(r.f.win_rate, 0) }, { key: "n", label: "windows", r: true, f: (r) => r.f.epochs },
      { key: "q", label: "screen quality diff", r: true, f: (r) => `${(r.q.mean_diff * 100).toFixed(1)} pts` }]} rows={rows} />
    {note && <p className="dim" style={{ marginTop: 6 }}>{note}</p>}
  </>);
}

export default function Learning() {
  const s = useApi<any>("/brain/summary", 15000);
  const main = useApi<any>(s.data?.main_report ? `/brain/reports/${s.data.main_report}` : null);
  const nul = useApi<any>(s.data?.null_report ? `/brain/reports/${s.data.null_report}?curves=false` : null);
  const ver = useApi<any>("/brain/verify");
  const [jobId, setJobId] = useState<string | null>(null);
  const { job, running } = useJob(jobId);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [appr, setAppr] = useState({ approver: "", reason: "" });
  const act = async (fn: () => Promise<any>, done?: (r: any) => void) => { setErr(null); setMsg(null); try { done?.(await fn()); s.reload(); } catch (e: any) { setErr(e.message); } };
  const d = s.data, r = main.data;
  const primary = r?.protocol?.primary_comparison;
  const sealed = r?.sealed_segment;
  const curveArms = primary ? Array.from(new Set([...primary, "baseline_random", "baseline_greedy"])) : [];
  const lift = d?.kpis?.find((k: any) => k.key === "oos_lift");
  const learner = r?.arms?.[primary?.[0]];
  return (
    <Page title="Learning Brain" sub="What the research system has learned, the evidence for it, which decisions it changed — and whether those changes improved results on data the system had not seen when it decided."
      right={<button className="primary" disabled={running} onClick={() => act(() => post("/brain/benchmark", { negative_control: true }), (j) => setJobId(j.id))}>{running ? "Benchmark running…" : "Run learning benchmark + negative control"}</button>}>
      <Err e={s.error || err} /><Loading on={s.loading} />
      {job && <Banner tone="info">Benchmark job <span className="mono">{job.id}</span>: <Badge s={job.status} /> {job.progress?.length ? `decision ${job.progress[job.progress.length - 1].epoch}/${job.progress[job.progress.length - 1].of}` : ""} {job.error}</Banner>}
      {d && <>
        {!lift || lift.status === "not measured"
          ? <Banner>No learning benchmark has been run on this server yet. Nothing here claims the system has learned anything until one has.</Banner>
          : <Banner tone={lift.status === "met" ? "good" : "crit"}><b>{lift.status === "met" ? "Learning is supported by out-of-sample evidence." : "Learning is NOT demonstrated."}</b> {r?.verdict} (basis: {r?.verdict_basis}).
            Storing results is not learning: the claim needs memory-informed decisions to beat the memory-free counterpart on windows no arm had seen.</Banner>}
        <Synthetic on={r?.dataset?.synthetic} />
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Champion policy" v={d.champion || "—"} h={`protocol v${d.protocol_version}`} />
          <Stat k="Episodes in memory" v={num(d.memory.episodes, 0)} h={`${d.memory.namespaces} namespaces`} />
          <Stat k="Memory chain" v={ver.data ? (ver.data.memory.ok ? "intact" : "BROKEN") : "…"} h="hash-chained, append-only" tone={ver.data?.memory.ok ? "up" : "down"} />
          {r && <><Stat k="Configurations" v={num(r.protocol.universe, 0)} h={`${r.protocol.budget_per_decision} screened per decision`} />
            <Stat k="Decision windows" v={r.protocol.epochs} h={`${r.protocol.first_decision} → ${r.protocol.last_window_end}`} />
            <Stat k="Screening throughput" v={r.evaluation.configs_per_second ? `${num(r.evaluation.configs_per_second, 0)}/s` : "cached"} h={`${r.evaluation.workers} workers`} /></>}
        </div>
        <Card title="Learning KPIs">
          <DataTable wrap maxHeight={520} cols={[{ key: "name", f: (k) => <b>{k.name}</b> }, { key: "status", f: (k) => <Badge s={k.status} tone={statusTone(k.status)} /> },
            { key: "value", label: "memory", r: true, f: (k) => fmtKpi(k.key, k.value) }, { key: "baseline", label: "memory-free", r: true, f: (k) => fmtKpi(k.key, k.baseline) },
            { key: "target" }, { key: "definition", label: "how it is measured" },
            { key: "evidence", f: (k) => k.evidence == null ? "" : typeof k.evidence === "string" ? k.evidence : k.evidence.ci90 ? (k.key === "research_efficiency" ? `diff CI90 ${(k.evidence.ci90[0] * 100).toFixed(1)}…${(k.evidence.ci90[1] * 100).toFixed(1)} pts, ${k.evidence.epochs} windows` : `CI90 ${(k.evidence.ci90[0] * 100).toFixed(3)}…${(k.evidence.ci90[1] * 100).toFixed(3)}%, ${k.evidence.epochs} windows`) : k.evidence.brier != null ? `Brier ${num(k.evidence.brier, 3)} vs base rate ${num(k.evidence.brier_base_rate, 3)}; AUC ${num(k.evidence.auc, 3)}` : "" }]} rows={d.kpis} />
        </Card>
      </>}
      {r && <>
        <div className="grid g21" style={{ marginTop: 14 }}>
          <Card title={`Forward (unseen) growth of 1 — ${r.label}`}>
            <LineChart x={r.arms[curveArms[0]].curve.dates} fmt={(v) => v.toFixed(3)} height={260}
              series={curveArms.filter((a) => r.arms[a]).map((a) => ({ name: a.replaceAll("_", " "), values: r.arms[a].curve.values }))} />
            <p className="dim">Every point is a return earned after the decision that produced it. Each arm screened the same number of configurations per decision.</p>
          </Card>
          <Card title="Self-validation (does each learned part earn its keep?)">
            {learner?.kpis?.self_validation ? <KV items={[
              ["learned screening", <span key="s"><Badge s={learner.kpis.self_validation.screening.lift > 0 ? "beats its random control" : "does not beat its random control"} tone={learner.kpis.self_validation.screening.lift > 0 ? "good" : "warn"} /> exploit hit {pct(learner.kpis.self_validation.screening.exploit_hit)} vs random {pct(learner.kpis.self_validation.screening.explore_hit)}</span>],
              ["meta-model", <span key="m"><Badge s={(learner.kpis.self_validation.meta_brier_skill ?? -1) > 0 ? "calibrated better than base rate" : "not trusted"} tone={(learner.kpis.self_validation.meta_brier_skill ?? -1) > 0 ? "good" : "warn"} /> Brier skill {num(learner.kpis.self_validation.meta_brier_skill, 3)}</span>],
              ["ranking approaches used", Object.entries(learner.approach_counts).map(([a, n]) => `${a} ×${n}`).join(", ")],
              ["decisions changed", learner.n_decision_changes]]} /> : <div className="empty">No learning arm in this report.</div>}
            <p className="dim" style={{ marginTop: 8 }}>A component that cannot beat a random control on data it had not seen falls back to memory-free behaviour.</p>
          </Card>
        </div>
        {sealed && <div style={{ marginTop: 14 }}><Card title={`Sealed window (used once): ${sealed.sealed_from} onwards, ${sealed.epochs} decisions`}><CompTable comps={sealed.comparisons} note="This is the verdict basis: the sealed window was opened once, after the protocol was frozen." /></Card></div>}
        <div style={{ marginTop: 14 }}><Card title={sealed ? "All windows (development + validation + sealed)" : "Development + validation windows"}><CompTable comps={r.comparisons} note={`Pre-declared comparisons for protocol v${r.protocol.protocol_version}; Holm-adjusted p accounts for testing all of them.`} /></Card></div>
        <div style={{ marginTop: 14 }}><Card title="Negative control: same protocol on a zero-drift random walk (nothing to learn)">
          {nul.data ? <><p><Badge s={nul.data.verdict.startsWith("no reliable") ? "no false positive" : "FALSE POSITIVE"} tone={nul.data.verdict.startsWith("no reliable") ? "good" : "crit"} /> {nul.data.verdict}</p><CompTable comps={nul.data.comparisons} /></> : <div className="empty">Not run on this server.</div>}
        </Card></div>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="What the brain has learned (findings that survive multiple-testing control)">
            <DataTable wrap maxHeight={420} cols={[{ key: "facet" }, { key: "value" }, { key: "regime" }, { key: "n", r: true },
              { key: "success_rate", label: "success", r: true, f: (x) => pct(x.success_rate) }, { key: "ci90", label: "90% credible", f: (x) => `${pct(x.ci90[0])}–${pct(x.ci90[1])}` },
              { key: "lift", label: "vs all", r: true, f: (x) => <span className={x.lift > 0 ? "up" : "down"}>{(x.lift * 100).toFixed(1)} pts</span>, sort: (x) => x.lift }]} rows={r.findings} empty="No finding survives multiple-testing control." />
            <p className="dim" style={{ marginTop: 6 }}>"Success" = forward return above the random-screen median in the same window. A finding is a pattern in past outcomes; it becomes useful only if it improves decisions on later data (see KPIs).</p>
          </Card>
          <Card title="Decisions that changed, and why">
            <DataTable wrap maxHeight={420} cols={[{ key: "date" }, { key: "type", f: (c) => c.type.replaceAll("_", " ") },
              { key: "change", f: (c) => c.type === "component" ? `${c.component.replaceAll("_", " ")} → ${c.enabled ? "ON" : "OFF"}` : `${JSON.stringify(c.from)} → ${JSON.stringify(c.to)}` },
              { key: "evidence", f: (c) => <span className="mono">{JSON.stringify(c.evidence).slice(0, 140)}</span> }]} rows={[...(learner?.decision_changes || [])].reverse()} empty="No decision changes recorded." />
          </Card>
        </div>
      </>}
      {d && <div style={{ marginTop: 14 }}><Card title="System versions (controlled self-modification)" right={<button onClick={() => act(() => post("/brain/versions/propose", { changes: null }), (x) => setMsg(x.rejected ? `Rejected: ${x.rejected}` : `Proposed ${x.version}: ${JSON.stringify(x.changes)}`))}>Ask the meta-optimiser for a challenger</button>}>
        {msg && <p>{msg}</p>}
        <DataTable wrap maxHeight={360} cols={[{ key: "id", f: (v) => <span className="mono">{v.id}</span> }, { key: "status", f: (v) => <Badge s={v.status} tone={v.status === "champion" ? "good" : ["rejected", "rolled_back"].includes(v.status) ? "crit" : "info"} /> },
          { key: "origin" }, { key: "changes", f: (v) => <span className="mono">{JSON.stringify(v.changes || {})}</span> },
          { key: "benchmark", label: "frozen benchmark", f: (v) => v.benchmark?.comparison?.mean_diff != null ? `${(v.benchmark.comparison.mean_diff * 100).toFixed(3)}%/window` : "—" },
          { key: "forward", label: "forward windows", r: true, f: (v) => v.forward?.length || 0 }, { key: "reason", f: (v) => v.reason || v.approved_by || "" }]} rows={d.versions} />
        {d.versions.some((v: any) => v.status === "awaiting_approval") && <div className="row" style={{ marginTop: 10 }}>
          <label>Your name<input value={appr.approver} onChange={(e) => setAppr({ ...appr, approver: e.target.value })} /></label>
          <label style={{ flex: 1 }}>Reason<input value={appr.reason} onChange={(e) => setAppr({ ...appr, reason: e.target.value })} style={{ width: "100%" }} /></label>
          {d.versions.filter((v: any) => v.status === "awaiting_approval").map((v: any) => <button key={v.id} className="primary" onClick={() => act(() => post(`/brain/versions/${v.id}/promote`, appr))}>Promote {v.id}</button>)}
        </div>}
        <h3>Promotions and rollbacks</h3>
        <DataTable maxHeight={200} cols={[{ key: "at" }, { key: "event", f: (h) => <Badge s={h.event} tone={h.event === "rolled_back" ? "crit" : "good"} /> }, { key: "version" }, { key: "detail", f: (h) => h.reason || (h.previous ? `replaced ${h.previous}` : "initial") }]} rows={[...d.history].reverse()} />
        <p className="dim" style={{ marginTop: 8 }}>Proposals touching broker credentials, execution permissions, risk limits or safety mechanisms are rejected automatically. Promotion needs a named human; regressions and failed correctness probes roll back automatically.</p>
      </Card></div>}
    </Page>
  );
}
