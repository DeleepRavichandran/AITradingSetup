"use client";
import Link from "next/link";
import { useState } from "react";
import { Badge, Banner, Card, DataTable, Err, Loading, Page } from "@/components/ui";
import { post, useApi, useJob } from "@/lib/api";

export default function Agents() {
  const a = useApi<any>("/research/agents", 4000);
  const jobs = useApi<any[]>("/jobs", 3000);
  const [f, setF] = useState({ max_iterations: 3, max_backtests: 300, max_wall_seconds: 900, max_sealed_evaluations: 1, walk_forward: true, seed: 0 });
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const active = jobs.data?.find((j) => j.kind === "research" && j.status === "running");
  const { job } = useJob(jobId || active?.id || null);
  const go = async () => { setErr(null); try { setJobId((await post("/research/run", f)).id); } catch (e: any) { setErr(e.message); } };
  const n = (k: keyof typeof f, label: string, max: number) => (
    <label>{label}<input type="number" min={0} max={max} value={f[k] as number} onChange={(e) => setF({ ...f, [k]: Number(e.target.value) })} style={{ width: 110 }} /></label>);
  return (
    <Page title="AI Research Agents" sub="Nine agents with separate jobs. They exchange typed artefacts through the registry; none of them can reach a broker, read credentials or change the live-trading flag.">
      <Err e={a.error || err} /><Loading on={a.loading} />
      <Card title="Start a budgeted research run">
        <div className="row">
          {n("max_iterations", "Hypotheses to test", 12)}{n("max_backtests", "Backtest budget", 3000)}{n("max_wall_seconds", "Time budget (s)", 7200)}{n("max_sealed_evaluations", "Sealed evaluations", 5)}{n("seed", "Seed", 99999)}
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}><input type="checkbox" checked={f.walk_forward} onChange={(e) => setF({ ...f, walk_forward: e.target.checked })} />walk-forward for finalists</label>
          <button className="primary" onClick={go} disabled={!!active}>{active ? "Running…" : "Run"}</button>
        </div>
        <p className="dim" style={{ marginTop: 8 }}>The run stops at whichever limit is reached first. The sealed window is opened only for hypotheses that already passed the audit, once each.</p>
        {job && <div style={{ marginTop: 10 }}>
          <p><Badge s={job.status} /> {job.error && <span className="down">{job.error}</span>}
            {job.status === "done" && <> {job.result.results.length} tested · {job.result.candidates.length} candidate(s) · {job.result.usage.backtests} backtests in {job.result.usage.wall_seconds}s · stop reason: {job.result.status} · <Link href="/factory">see results</Link></>}</p>
          {job.status === "done" && job.result.inventor_log?.length > 0 && <Banner>{job.result.inventor_log.join(" · ")}</Banner>}
          {job.status === "running" && <DataTable maxHeight={200} cols={[{ key: "sender" }, { key: "recipient" }, { key: "task" }, { key: "hypothesis_id", label: "hypothesis" }]} rows={[...job.progress].reverse()} empty="Starting…" />}
        </div>}
      </Card>
      {a.data && <>
        <div style={{ marginTop: 14 }}><Card title="The team">
          <DataTable wrap maxHeight={520} cols={[{ key: "name", f: (r) => <b>{r.name.replaceAll("_", " ")}</b> }, { key: "role" }, { key: "implementation" }, { key: "messages_sent", label: "messages", r: true },
            { key: "last_task", label: "last task", f: (r) => r.last_task || "—" }, { key: "last_active", label: "last active", f: (r) => (r.last_active || "—").replace("T", " ") }]} rows={a.data.agents} />
          <h3>Boundaries enforced in code</h3>
          <ul style={{ margin: 0, paddingLeft: 18 }}>{a.data.boundaries.map((b: string) => <li key={b}>{b}</li>)}</ul>
        </Card></div>
        <div style={{ marginTop: 14 }}><Card title="Message log (newest first)">
          <DataTable maxHeight={420} cols={[{ key: "created_at", label: "time", f: (r) => r.created_at.replace("T", " ") }, { key: "sender" }, { key: "recipient" }, { key: "task_type", label: "task" },
            { key: "hypothesis_id", label: "hypothesis", f: (r) => (r.hypothesis_id ? <Link href={`/factory?h=${r.hypothesis_id}`}>{r.hypothesis_id}</Link> : "—") },
            { key: "payload", f: (r) => <span className="mono">{JSON.stringify(r.payload).slice(0, 110)}</span> }]} rows={a.data.recent_messages} empty="No messages yet." />
        </Card></div>
      </>}
    </Page>
  );
}
