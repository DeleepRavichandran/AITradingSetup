"use client";
import { useState } from "react";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page } from "@/components/ui";
import { AccountKinds } from "@/components/kinds";
import { Usage } from "@/components/vega";
import { inr, post, useApi } from "@/lib/api";

function LiveUsage() {
  const rd = useApi<any>("/radar", 30000);
  const l = rd.data?.limits;
  return (
    <Card title="Live paper account: limits in use">
      {rd.error ? <p className="dim" style={{ margin: 0 }}>No live paper account is running ({rd.error}).</p> : <>
        <Usage usage={l?.usage} />
        <p className="ink2" style={{ margin: "10px 0 0", fontSize: 13 }}>{l?.blocked?.length ? `New buys blocked: ${l.blocked.join("; ")}.` : "No portfolio-wide block on new buys."} These limits
          are the operator's; no AI agent can change them, and the integrity layer can only tighten (suspend), never loosen.</p></>}
    </Card>
  );
}

function TradeLimit() {
  const a = useApi<any>("/risk/extra-trades", 15000);
  const [n, setN] = useState("2");
  const [why, setWhy] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const d = a.data;
  const approve = async () => {
    setMsg(null);
    try { const r = await post("/risk/extra-trades", { extra: Number(n), reason: why }); setMsg(`Approved: ${r.approved_today} extra trade(s) allowed today.`); setWhy(""); a.reload(); }
    catch (e: any) { setMsg(`Not approved: ${e.message}`); }
  };
  return (
    <Card title="Daily trade limit">
      <p data-testid="trade-limit" style={{ marginTop: 0 }}>
        New trades today: <b>{d?.entries_today ?? "—"}</b> of <b>{d?.entries_allowed_today ?? 10}</b> allowed.
        The bot may open at most 10 new trades a day. More only with your approval and a written reason; exits are never blocked.</p>
      <div className="row">
        <label>Extra trades<input id="extra-trades" type="number" min={1} max={20} value={n} onChange={(e) => setN(e.target.value)} style={{ width: 80 }} /></label>
        <label style={{ flex: 1 }}>Strong reason (at least 20 characters)<input id="extra-reason" value={why} onChange={(e) => setWhy(e.target.value)} style={{ width: "100%" }} /></label>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button disabled={why.trim().length < 20 || !(Number(n) >= 1 && Number(n) <= 20)} onClick={approve}>Allow {n} more today</button>
      </div>
      {msg && <p role="status" className="ink2">{msg}</p>}
      {d?.approvals?.length > 0 && <DataTable wrap maxHeight={160} rows={d.approvals} cols={[{ key: "at", f: (x: any) => String(x.at).slice(11, 16) }, { key: "extra", r: true }, { key: "reason" }]} />}
    </Card>
  );
}

export default function Risk() {
  const r = useApi<any>("/risk", 4000);
  const [f, setF] = useState({ operator: "", reason: "" });
  const [err, setErr] = useState<string | null>(null);
  const act = async (path: string) => { setErr(null); try { await post(path, f); r.reload(); } catch (e: any) { setErr(e.message); } };
  const d = r.data;
  return (
    <Page title="Risk Control" sub="The kill switch stops every paper session and blocks every new order, paper or live. Engaging it is immediate; clearing it needs a named human and a reason, and is written to the audit log.">
      <AccountKinds here="screening" />
      <Err e={r.error || err} /><Loading on={r.loading} />
      <div style={{ marginBottom: 14 }}><LiveUsage /></div>
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
        <div style={{ marginTop: 14 }}><TradeLimit /></div>
        <div style={{ marginTop: 14 }}><Card title="Paper sessions">
          <DataTable cols={[{ key: "session_id", label: "session" }, { key: "halted", label: "state", f: (x) => <Badge s={x.halted ? "halted" : "running"} tone={x.halted ? "crit" : "good"} /> }, { key: "why", f: (x) => x.halted || "—" },
            { key: "equity", r: true, f: (x) => inr(x.equity) }, { key: "capital", r: true, f: (x) => inr(x.capital) }, { key: "positions", r: true }, { key: "open_orders", label: "working orders", r: true }]} rows={d.sessions} empty="No paper sessions running." />
        </Card></div>
      </>}
    </Page>
  );
}
