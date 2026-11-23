"use client";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page } from "@/components/ui";
import { useApi } from "@/lib/api";

export default function Brokers() {
  const b = useApi<any>("/brokers");
  const g = useApi<any>("/execution/gate");
  return (
    <Page title="Broker Connections" sub="Market-data connectivity and order routing are separate adapters. An adapter marked UNVERIFIED has only been tested against a mock; NOT IMPLEMENTED means the code does not exist.">
      <Err e={b.error} /><Loading on={b.loading} />
      {b.data && <>
        <Banner tone={b.data.live_trading_enabled ? "crit" : "good"}>{b.data.live_trading_enabled ? "LIVE_TRADING_ENABLED is true in the server environment. Orders still require a signed approval, a complete compliance checklist and a clear kill switch." : "Live order routing is disabled. No adapter can send an order."}</Banner>
        <Card title="Order-routing adapters">
          <DataTable wrap cols={[{ key: "name" }, { key: "status", f: (r) => <Badge s={r.status.split(" ")[0] === "NOT" ? "NOT IMPLEMENTED" : r.status.split(" ")[0]} /> }, { key: "credentials_present", label: "credentials", f: (r) => (r.credentials_present === undefined ? "—" : r.credentials_present ? "present" : "not set") },
            { key: "order_types", label: "order types", f: (r) => r.order_types.join(", ") || "—" }, { key: "products", f: (r) => r.products.join(", ") || "—" }, { key: "token_lifetime", label: "session", f: (r) => r.token_lifetime || "—" },
            { key: "requires_static_ip", label: "static IP", f: (r) => (r.requires_static_ip ? "required" : "no") }, { key: "notes", f: (r) => `${r.status.includes("(") ? r.status.slice(r.status.indexOf("(")) + " " : ""}${r.notes}` }]} rows={b.data.adapters} />
        </Card>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="Market data"><DataTable cols={[{ key: "name" }, { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "credentials_present", label: "credentials", f: (r) => (r.credentials_present === undefined ? "—" : r.credentials_present ? "present" : "not set") }]} rows={b.data.market_data} /></Card>
          <Card title="Live gate (state for an unapproved order)">
            <p><Badge s={b.data.gate_default.allowed ? "open" : "closed"} tone={b.data.gate_default.allowed ? "crit" : "good"} /></p>
            <ul style={{ margin: 0, paddingLeft: 18 }}>{b.data.gate_default.reasons.map((r: string) => <li key={r}>{r}</li>)}</ul>
            {g.data && <><h3>How a strategy gets approved</h3><p>{g.data.how_to_approve}.</p>
              <KV items={Object.entries(g.data.compliance).map(([k, v]) => [k.replaceAll("_", " "), <Badge key={k} s={v ? "confirmed" : "open"} tone={v ? "good" : "warn"} />])} /></>}
          </Card>
        </div>
        <p className="dim" style={{ marginTop: 12 }}>{b.data.note}.</p>
      </>}
    </Page>
  );
}
