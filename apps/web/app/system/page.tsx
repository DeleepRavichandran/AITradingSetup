"use client";
import { useState } from "react";
import { Badge, Card, DataTable, Err, Issues, KV, Loading, Page, Stat, na } from "@/components/ui";
import { post, useApi } from "@/lib/api";

export default function System() {
  const s = useApi<any>("/system", 5000);
  const jobs = useApi<any[]>("/jobs", 3000);
  const [note, setNote] = useState<string | null>(null);
  const test = async () => { try { const r = await post("/notifications/test"); setNote(r.delivered ? "Delivered." : `Not delivered: ${r.recent?.slice(-1)[0]?.detail || r.status}`); s.reload(); } catch (e: any) { setNote(e.message); } };
  const d = s.data;
  return (
    <Page title="Operations" sub="Health of the service, the sandbox that runs generated code, background jobs and notification delivery.">
      <Err e={s.error} /><Loading on={s.loading} />
      {d && <>
        <Issues testid="ops-issues" items={[
          ...(d.sandbox && !d.sandbox.network_namespace ? [{ issue: "The sandbox for generated code is unavailable", affects: "Strategy Code, AI Research Agents, Command Center research run", tone: "crit" as const }] : []),
          ...(d.notifications && d.notifications.status !== "configured" && d.notifications.status !== "ok" ? [{ issue: `WhatsApp notifications: ${d.notifications.status}`, affects: "alerts (kill switch, feed, daily summary)" }] : []),
          ...((jobs.data || []).filter((j: any) => j.status === "error").slice(-5).map((j: any) => ({ issue: `Job ${j.kind} failed: ${j.error || "no detail"}`, affects: j.kind, tone: "crit" as const }))),
          ...((d.features || []).filter((f: any) => /unavailable|degraded|error|blocked/i.test(String(f.status))).map((f: any) => ({ issue: `${f.feature}: ${f.status}`, affects: f.feature }))),
          ...(d.storage?.disk_free_gb != null && d.storage.disk_free_gb < 2 ? [{ issue: `Low disk space: ${d.storage.disk_free_gb} GB free`, affects: "ledgers, datasets, checkpoints", tone: "crit" as const }] : []),
        ]} />
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Uptime" v={na(d.uptime_seconds, (x) => `${Math.floor(x / 60)} min`)} /><Stat k="Version" v={na(d.version)} h={d.code_version} /><Stat k="Jobs running" v={na(d.counts?.jobs_running)} />
          <Stat k="Registry size" v={na(d.storage?.registry_mb, (x) => `${x} MB`)} /><Stat k="Disk free" v={na(d.storage?.disk_free_gb, (x) => `${x} GB`)} />
          <Stat k="API auth" v={d.auth_required ? "token required" : "local only"} h={d.auth ? `role: ${d.auth.role}` : ""} tone={d.auth_required ? "up" : "down"} />
        </div>
        <details className="panel" style={{ marginBottom: 14 }}><summary>Sandbox and notification details</summary>
          <div className="grid g2" style={{ marginTop: 10 }}>
            <Card title="Sandbox for generated code">
              {d.sandbox ? <KV items={[["isolation", <Badge key="n" s={d.sandbox.network_namespace ? "network + filesystem + PID isolated" : "UNAVAILABLE — generated code will not run"} tone={d.sandbox.network_namespace ? "good" : "crit"} />], ["detail", d.sandbox.detail], ["CPU limit", na(d.sandbox.cpu_seconds, (x) => `${x}s`)], ["memory limit", na(d.sandbox.memory_mb, (x) => `${x} MB`)],
                ["wall-clock limit", na(d.sandbox.wall_seconds, (x) => `${x}s`)], ["environment", "scrubbed: no API keys, broker tokens or approval secret"], ["execution package", "refuses to import inside the sandbox"]]} /> : na(null)}
            </Card>
            <Card title="WhatsApp notifications" right={<button onClick={test}>Send test</button>}>
              {d.notifications ? <><KV items={[["status", <Badge key="s" s={d.notifications.status} />], ["recipients", na(d.notifications.recipients)], ["mode", na(d.notifications.mode)], ["Graph API version", na(d.notifications.api_version)]]} />
                {note && <p style={{ marginTop: 8 }}>{note}</p>}
                <DataTable wrap maxHeight={170} cols={[{ key: "ts", f: (r) => r.ts.replace("T", " ") }, { key: "event" }, { key: "ok", label: "sent", f: (r) => <Badge s={r.ok ? "delivered" : "not sent"} tone={r.ok ? "good" : "warn"} /> }, { key: "text" }, { key: "detail" }]} rows={[...(d.notifications.recent || [])].reverse()} empty="No notifications attempted yet." /></> : na(null)}
            </Card>
          </div>
        </details>
        <details className="panel" style={{ marginBottom: 14 }}><summary>Background jobs ({(jobs.data || []).length})</summary>
          <DataTable maxHeight={260} cols={[{ key: "id", f: (r) => <span className="mono">{r.id}</span> }, { key: "kind" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "started", f: (r) => r.started.replace("T", " ") },
            { key: "finished", f: (r) => (r.finished || "—").replace("T", " ") }, { key: "error", f: (r) => r.error || "" }]} rows={jobs.data || []} empty="No jobs since the service started." />
        </details>
        <details className="panel"><summary>Capability status ({(d.features || []).length})</summary>
          <DataTable wrap maxHeight={520} cols={[{ key: "feature" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "note" }]} rows={d.features || []} />
        </details>
      </>}
    </Page>
  );
}
