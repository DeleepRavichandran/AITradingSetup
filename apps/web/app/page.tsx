"use client";
import Link from "next/link";
import { useState } from "react";
import { MetricTiles, runCols } from "@/components/common";
import { Badge, Banner, Card, DataTable, Err, Loading, Page, Stat } from "@/components/ui";
import { post, useApi, useJob } from "@/lib/api";

export default function CommandCenter() {
  const sys = useApi<any>("/system", 10000);
  const runs = useApi<any[]>("/runs?limit=8", 10000);
  const cands = useApi<any[]>("/research/hypotheses?status=paper_candidate", 15000);
  const mem = useApi<any>("/research/memory", 15000);
  const jobs = useApi<any[]>("/jobs", 3000);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, running } = useJob(jobId);
  const active = jobs.data?.find((j) => j.kind === "research" && j.status === "running");
  const start = async () => {
    setErr(null);
    try { setJobId((await post("/research/run", { max_iterations: 3, max_sealed_evaluations: 1 })).id); } catch (e: any) { setErr(e.message); }
  };
  const s = sys.data;
  const last = runs.data?.[0];
  return (
    <Page title="Command Center" sub="State of the lab right now: what data is loaded, what the research team has tested, what is running, and which capabilities are real, unverified or blocked."
      right={<button className="primary" onClick={start} disabled={!!active || running}>{active || running ? "Research running…" : "Run research (3 hypotheses)"}</button>}>
      <Err e={sys.error || err} /><Loading on={sys.loading} />
      {s && <>
        {!s.auth_required && <Banner>API authentication is off. Set NSEQL_API_TOKEN before exposing this dashboard beyond your own machine.</Banner>}
        {s.kill_switch.engaged && <Banner tone="crit"><b>Kill switch engaged:</b> {s.kill_switch.reason}. <Link href="/risk">Risk Control Center</Link></Banner>}
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Backtests recorded" v={s.counts.runs} h={`${s.counts.trials_for_dsr} count as trials`} />
          <Stat k="Hypotheses tested" v={s.counts.hypotheses} h={`${s.counts.experiments} research runs`} />
          <Stat k="Paper candidates" v={cands.data?.length ?? "—"} h="passed audit + sealed check" />
          <Stat k="Lessons recorded" v={s.counts.lessons} />
          <Stat k="Paper sessions" v={s.counts.paper_sessions} h="in memory" />
          <Stat k="Datasets" v={s.datasets.length} h={s.datasets.join(", ")} />
          <Stat k="LLM" v={s.llm.configured ? "configured" : "off"} h={s.llm.configured ? s.llm.model : "deterministic agents"} />
          <Stat k="Live orders" v={s.live_trading_enabled ? "FLAG ON" : "disabled"} h="gate closed by default" />
        </div>
        {(active || jobId) && <Card title="Research run in progress">
          <p><Badge s={job?.status || "running"} /> {job?.error && <span className="down">{job.error}</span>}</p>
          <DataTable maxHeight={180} cols={[{ key: "sender" }, { key: "recipient" }, { key: "task" }, { key: "hypothesis_id", label: "hypothesis" }]} rows={[...((job || active)?.progress || [])].reverse()} empty="Waiting for the first agent message…" />
          {job?.status === "done" && <p style={{ marginTop: 8 }}>Finished: {job.result.results.length} hypotheses, {job.result.candidates.length} candidate(s). <Link href="/factory">Open the Strategy Factory</Link></p>}
        </Card>}
        <div className="grid g21" style={{ marginTop: 14 }}>
          <Card title="Latest runs" right={<Link href="/compare">compare</Link>}>
            <DataTable cols={runCols().filter((c) => !["kind", "created_at", "data"].includes(c.key))} rows={runs.data} maxHeight={300} empty="No backtests yet. Run one in the Backtesting Studio." />
          </Card>
          <Card title="What the team learned last" right={<Link href="/memory">memory</Link>}>
            {mem.data?.lessons?.length ? mem.data.lessons.slice(0, 5).map((l: any) => (
              <p key={l.id} style={{ fontSize: 12.5 }}><Badge s={l.outcome} /> {l.text} <span className="dim">Next: {l.next_step}</span></p>)) : <div className="empty">No lessons yet — run research.</div>}
          </Card>
        </div>
        {last && <div style={{ marginTop: 14 }}><Card title={<>Most recent run · <Link href={`/backtest?run=${last.run_id}`} className="mono">{last.run_id}</Link> · {last.strategy}</>}>
          {last.synthetic ? <p className="dim">Synthetic data.</p> : null}<MetricTiles m={last.metrics} /></Card></div>}
        <div style={{ marginTop: 14 }}><Card title="Capability status (reported by the backend)">
          <DataTable maxHeight={420} wrap cols={[{ key: "feature" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "note" }]} rows={s.features} />
        </Card></div>
      </>}
    </Page>
  );
}
