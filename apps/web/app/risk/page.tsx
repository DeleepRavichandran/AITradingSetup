"use client";
import { useState } from "react";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page } from "@/components/ui";
import { inr, post, useApi } from "@/lib/api";

export default function Risk() {
  const r = useApi<any>("/risk", 4000);
  const [f, setF] = useState({ operator: "", reason: "" });
  const [err, setErr] = useState<string | null>(null);
  const act = async (path: string) => { setErr(null); try { await post(path, f); r.reload(); } catch (e: any) { setErr(e.message); } };
  const d = r.data;
  return (
    <Page title="Risk Control Center" sub="The kill switch stops every paper session and blocks every new order, paper or live. Engaging it is immediate; clearing it needs a named human and a reason, and is written to the audit log.">
      <Err e={r.error || err} /><Loading on={r.loading} />
      {d && <>
        <Banner tone={d.kill_switch.engaged ? "crit" : "good"}>{d.kill_switch.engaged ? <><b>Kill switch ENGAGED.</b> {d.kill_switch.reason}</> : "Kill switch clear."}</Banner>
        <div className="grid g2">
          <Card title="Kill switch">
            <div className="row">
              <label>Your name<input value={f.operator} onChange={(e) => setF({ ...f, operator: e.target.value })} /></label>
              <label style={{ flex: 1 }}>Reason<input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} style={{ width: "100%" }} /></label>
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="danger" disabled={f.operator.length < 2 || f.reason.length < 3 || d.kill_switch.engaged} onClick={() => act("/risk/kill")}>Engage kill switch</button>
              <button disabled={f.operator.length < 2 || f.reason.length < 3 || !d.kill_switch.engaged} onClick={() => act("/risk/reset")}>Reset (human only)</button>
            </div>
            <h3>History</h3>
            <DataTable wrap maxHeight={200} cols={[{ key: "ts", f: (x) => String(x.ts).slice(0, 19) }, { key: "event", f: (x) => <Badge s={x.event} tone={x.event === "ENGAGED" ? "crit" : "good"} /> }, { key: "operator", f: (x) => x.operator || "—" }, { key: "reason" }]} rows={[...d.kill_switch.history].reverse()} empty="Never engaged in this process." />
            <p className="dim" style={{ marginTop: 8 }}>Also engaged automatically by: a stale or failed market-data feed, a breached session loss limit, a failed ledger reconciliation, or five consecutive rejected orders.</p>
          </Card>
          <Card title="Live-order gate">
            <p><Badge s={d.gate.allowed ? "open" : "closed"} tone={d.gate.allowed ? "crit" : "good"} /> LIVE_TRADING_ENABLED = {String(d.live_trading_enabled)}</p>
            <ul style={{ margin: 0, paddingLeft: 18 }}>{d.gate.reasons.map((x: string) => <li key={x}>{x}</li>)}</ul>
            <h3>Default pre-trade limits</h3>
            <KV items={[["max order value", inr(d.limits.max_order_notional)], ["max position per symbol", inr(d.limits.max_position_notional)], ["max gross leverage", `${d.limits.max_gross_leverage}×`],
              ["daily loss halt", d.limits.max_daily_loss_pct >= 1 ? "off by default (set per run)" : `${d.limits.max_daily_loss_pct * 100}%`], ["orders per day per book", d.limits.max_orders_per_day],
              ["overnight equity shorts", d.limits.allow_equity_short_overnight ? "allowed" : "blocked (intraday only)"]]} />
          </Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Paper sessions">
          <DataTable cols={[{ key: "session_id", label: "session" }, { key: "halted", label: "state", f: (x) => <Badge s={x.halted ? "halted" : "running"} tone={x.halted ? "crit" : "good"} /> }, { key: "why", f: (x) => x.halted || "—" },
            { key: "equity", r: true, f: (x) => inr(x.equity) }, { key: "capital", r: true, f: (x) => inr(x.capital) }, { key: "positions", r: true }, { key: "open_orders", label: "working orders", r: true }]} rows={d.sessions} empty="No paper sessions running." />
        </Card></div>
      </>}
    </Page>
  );
}
