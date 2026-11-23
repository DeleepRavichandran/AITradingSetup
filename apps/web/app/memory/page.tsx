"use client";
import Link from "next/link";
import { useState } from "react";
import { Bars } from "@/components/charts";
import { runCols } from "@/components/common";
import { Badge, Card, DataTable, Err, KV, Loading, Page, Stat } from "@/components/ui";
import { useApi } from "@/lib/api";

export default function Memory() {
  const m = useApi<any>("/research/memory", 10000);
  const ex = useApi<any[]>("/research/experiments", 10000);
  const [id, setId] = useState<string | null>(null);
  const eid = id || ex.data?.[0]?.id || null;
  const d = useApi<any>(eid ? `/research/experiments/${eid}` : null);
  const e = d.data?.experiment;
  return (
    <Page title="Experiment Memory" sub="The permanent record: every experiment, what it cost, what was concluded and what should be tried next. Hypothesis-generating agents read this memory — minus anything measured on the sealed window.">
      <Err e={m.error} /><Loading on={m.loading} />
      {m.data && <>
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Trials counted" v={m.data.trial_count} h="denominator for deflated Sharpe" /><Stat k="Lessons" v={m.data.lessons.length} /><Stat k="Sealed evaluations used" v={m.data.sealed_evaluations.length} h="one per hypothesis, ever" />
          {Object.entries(m.data.runs_by_partition).map(([k, v]: any) => <Stat key={k} k={`Runs on ${k}`} v={v} />)}
        </div>
        <div className="grid g21">
          <Card title="Lessons (newest first)">
            <DataTable wrap maxHeight={420} cols={[{ key: "created_at", label: "when", f: (r) => r.created_at.replace("T", " ") }, { key: "outcome", f: (r) => <Badge s={r.outcome} /> },
              { key: "text", label: "what was learned" }, { key: "next_step", label: "next experiment" }, { key: "hypothesis_id", label: "hypothesis", f: (r) => (r.hypothesis_id ? <Link href={`/factory?h=${r.hypothesis_id}`}>{r.hypothesis_id}</Link> : "—") }]} rows={m.data.lessons} empty="No lessons yet." />
          </Card>
          <Card title="Hypotheses tested per family">
            <Bars items={Object.entries(m.data.families).map(([label, v]: any) => ({ label, value: v.tested }))} fmt={(v) => String(v)} />
            <h3>Survived audit</h3><Bars items={Object.entries(m.data.families).map(([label, v]: any) => ({ label, value: v.survived }))} fmt={(v) => String(v)} />
            <h3>Sealed-set access log</h3>
            <DataTable maxHeight={150} cols={[{ key: "created_at", label: "when", f: (r) => r.created_at.replace("T", " ") }, { key: "hypothesis_id", label: "hypothesis" }, { key: "requester" }]} rows={m.data.sealed_evaluations} empty="The sealed window has never been opened." />
          </Card>
        </div>
      </>}
      <div style={{ marginTop: 14 }}><Card title="Research runs">
        <DataTable maxHeight={220} onRow={(r) => setId(r.id)} selected={(r) => r.id === eid} cols={[{ key: "id", f: (r) => <span className="mono">{r.id}</span> }, { key: "created_at", label: "started", f: (r) => r.created_at.replace("T", " ") },
          { key: "status", f: (r) => <Badge s={r.status.split(":")[0]} /> }, { key: "tested", r: true, f: (r) => r.usage?.iterations ?? "—" }, { key: "backtests", r: true, f: (r) => r.usage?.backtests ?? "—" },
          { key: "sealed", r: true, f: (r) => r.usage?.sealed_evaluations ?? "—" }, { key: "seconds", r: true, f: (r) => r.usage?.wall_seconds ?? "—" }, { key: "candidates", f: (r) => (r.summary?.candidates || []).join(", ") || "—" }]} rows={ex.data} empty="No research runs yet." />
      </Card></div>
      {e && <div className="grid g2" style={{ marginTop: 14 }}>
        <Card title={<>Run <span className="mono">{e.id}</span></>}>
          <KV items={[["status", e.status], ["budget", <span key="b" className="mono">{JSON.stringify(e.budget)}</span>], ["used", <span key="u" className="mono">{JSON.stringify(e.usage)}</span>], ["inventor", e.summary?.inventor || "—"],
            ["partition", e.summary?.partition ? `dev → ${e.summary.partition.dev_end} · validation → ${e.summary.partition.val_end} · sealed → ${e.summary.partition.sealed_end}` : "—"], ["next experiment", e.summary?.next_experiment || "—"]]} />
          <h3>Outcomes</h3>
          <DataTable wrap maxHeight={260} cols={[{ key: "hypothesis_id", label: "hypothesis", f: (r) => <Link href={`/factory?h=${r.hypothesis_id}`}>{r.hypothesis_id}</Link> }, { key: "family" }, { key: "decision", f: (r) => <Badge s={r.decision} /> }, { key: "reasons", f: (r) => r.reasons.join(", ") || "—" }]} rows={e.summary?.results} empty="No hypotheses evaluated." />
        </Card>
        <Card title={`Agent messages (${d.data.messages.length})`}>
          <DataTable maxHeight={420} cols={[{ key: "id", r: true }, { key: "sender" }, { key: "recipient" }, { key: "task_type", label: "task" }, { key: "hypothesis_id", label: "hypothesis", f: (r) => r.hypothesis_id || "—" }]} rows={d.data.messages} />
        </Card>
      </div>}
      {d.data?.runs?.length > 0 && <div style={{ marginTop: 14 }}><Card title={`Backtests in this run (${d.data.runs.length})`}><DataTable cols={runCols().filter((c) => c.key !== "data")} rows={d.data.runs} maxHeight={300} /></Card></div>}
    </Page>
  );
}
