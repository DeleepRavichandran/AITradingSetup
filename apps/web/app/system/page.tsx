"use client";
import { useState } from "react";
import { Badge, Card, DataTable, Err, KV, Loading, Page, Stat } from "@/components/ui";
import { post, useApi } from "@/lib/api";

export default function System() {
  const s = useApi<any>("/system", 5000);
  const jobs = useApi<any[]>("/jobs", 3000);
  const [note, setNote] = useState<string | null>(null);
  const test = async () => { try { const r = await post("/notifications/test"); setNote(r.delivered ? "Delivered." : `Not delivered: ${r.recent?.slice(-1)[0]?.detail || r.status}`); s.reload(); } catch (e: any) { setNote(e.message); } };
  const d = s.data;
  return (
    <Page title="System Monitoring" sub="Health of the service, the sandbox that runs generated code, background jobs and notification delivery.">
      <Err e={s.error} /><Loading on={s.loading} />
      {d && <>
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Uptime" v={`${Math.floor(d.uptime_seconds / 60)} min`} /><Stat k="Version" v={d.version} h={d.code_version} /><Stat k="Jobs running" v={d.counts.jobs_running} />
          <Stat k="Registry size" v={`${d.storage.registry_mb} MB`} /><Stat k="Disk free" v={`${d.storage.disk_free_gb} GB`} /><Stat k="API auth" v={d.auth_required ? "token required" : "OFF"} tone={d.auth_required ? "up" : "down"} />
        </div>
        <div className="grid g2">
          <Card title="Sandbox for generated code">
            <KV items={[["network namespace", <Badge key="n" s={d.sandbox.network_namespace ? "isolated" : "NOT isolated on this host"} tone={d.sandbox.network_namespace ? "good" : "warn"} />], ["CPU limit", `${d.sandbox.cpu_seconds}s`], ["memory limit", `${d.sandbox.memory_mb} MB`],
              ["wall-clock limit", `${d.sandbox.wall_seconds}s`], ["environment", "scrubbed: no API keys, broker tokens or approval secret"], ["execution package", "refuses to import inside the sandbox"]]} />
          </Card>
          <Card title="WhatsApp notifications" right={<button onClick={test}>Send test</button>}>
            <KV items={[["status", <Badge key="s" s={d.notifications.status} />], ["recipients", d.notifications.recipients], ["mode", d.notifications.mode], ["Graph API version", d.notifications.api_version]]} />
            {note && <p style={{ marginTop: 8 }}>{note}</p>}
            <DataTable wrap maxHeight={170} cols={[{ key: "ts", f: (r) => r.ts.replace("T", " ") }, { key: "event" }, { key: "ok", label: "sent", f: (r) => <Badge s={r.ok ? "delivered" : "not sent"} tone={r.ok ? "good" : "warn"} /> }, { key: "text" }, { key: "detail" }]} rows={[...d.notifications.recent].reverse()} empty="No notifications attempted yet." />
          </Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Background jobs">
          <DataTable maxHeight={260} cols={[{ key: "id", f: (r) => <span className="mono">{r.id}</span> }, { key: "kind" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "started", f: (r) => r.started.replace("T", " ") },
            { key: "finished", f: (r) => (r.finished || "—").replace("T", " ") }, { key: "error", f: (r) => r.error || "" }]} rows={jobs.data} empty="No jobs since the service started." />
        </Card></div>
        <div style={{ marginTop: 14 }}><Card title="Capability status">
          <DataTable wrap maxHeight={520} cols={[{ key: "feature" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "note" }]} rows={d.features} />
        </Card></div>
      </>}
    </Page>
  );
}
