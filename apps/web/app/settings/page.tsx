"use client";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page } from "@/components/ui";
import { setToken, useApi } from "@/lib/api";

export default function Settings() {
  const s = useApi<any>("/settings");
  const a = useApi<any[]>("/audit?limit=400", 8000);
  const d = s.data;
  return (
    <Page title="Settings & Audit Logs" sub="Configuration is read from the server environment at start-up and shown here read-only. Safety-critical settings cannot be changed from the browser, the API, or by an agent."
      right={<button onClick={() => { setToken(""); window.location.reload(); }}>Sign out</button>}>
      <Err e={s.error} /><Loading on={s.loading} />
      {d && <div className="grid g2">
        <Card title="Safety">
          <Banner tone={d.live_trading_enabled ? "crit" : "good"}>LIVE_TRADING_ENABLED = <b>{String(d.live_trading_enabled)}</b>. {d.live_trading_note}.</Banner>
          <KV items={[["API authentication", d.auth_required ? "token required" : "off"], ["Anthropic credentials", d.credentials.anthropic ? "present" : "not set"], ["Kite credentials", d.credentials.kite ? "present" : "not set"],
            ["WhatsApp credentials", d.credentials.whatsapp ? "present" : "not set"], ["approval secret visible to this service", d.credentials.human_approval_secret_visible_to_api ? "yes — required only if you intend to verify live approvals" : "no (live approvals cannot be verified, so the gate stays closed)"],
            ["data directory", d.data_dir], ["experiments directory", d.experiments_dir]]} />
        </Card>
        <Card title="Compliance checklist (operator-maintained file)">
          <DataTable wrap maxHeight={330} cols={[{ key: "done", label: "state", f: (r) => <Badge s={r.done ? "confirmed" : "open"} tone={r.done ? "good" : "warn"} /> }, { key: "text", label: "item" }]} rows={d.compliance} />
          <p className="dim" style={{ marginTop: 8 }}>Edit <span className="mono">experiments/compliance.json</span> on the server to confirm items. All must be confirmed before the live gate can open.</p>
        </Card>
      </div>}
      <div style={{ marginTop: 14 }}><Card title="Audit log (newest first)">
        <DataTable maxHeight={560} cols={[{ key: "created_at", label: "time", f: (r) => r.created_at.replace("T", " ") }, { key: "actor" }, { key: "action" }, { key: "detail", f: (r) => <span className="mono">{JSON.stringify(r.detail).slice(0, 180)}</span> }]} rows={a.data} empty="Nothing logged yet." />
      </Card></div>
    </Page>
  );
}
