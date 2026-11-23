"use client";
import { useState } from "react";
import { LineChart } from "@/components/charts";
import { Badge, Banner, Card, DataSource, DataTable, Err, KV, Page, Stat, Synthetic } from "@/components/ui";
import { AccountKinds } from "@/components/kinds";
import { inr, num, pct, post, useApi } from "@/lib/api";

export default function Paper() {
  const strategies = useApi<any>("/strategies");
  const list = useApi<any[]>("/paper/sessions", 5000);
  const [sid, setSid] = useState<string | null>(null);
  const [st, setSt] = useState<any>(null);
  const [f, setF] = useState({ strategy: "sma_cross", window: "sealed", capital: 1000000, feed: "replay", symbols: "", auto_bars: 0 });
  const sched = useApi<any>("/paper/scheduler", 10000);
  const [o, setO] = useState({ symbol: "SYNBANK", qty: 10, type: "MARKET", limit: "" });
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useApi<any>(sid && !st ? `/paper/sessions/${sid}` : null);
  const s = st || live.data;
  const call = async (fn: () => Promise<any>, after?: (r: any) => void) => { setBusy(true); setErr(null); setMsg(null); try { after?.(await fn()); } catch (e: any) { setErr(e.message); } setBusy(false); };
  const start = () => call(() => post("/paper/sessions", { strategy: f.strategy || null, window: f.window, capital: f.capital, feed: f.feed, auto_bars: f.auto_bars,
    symbols: f.symbols.split(/[\s,]+/).filter(Boolean) }), (r) => { setSid(r.session_id); setSt(r); list.reload(); });
  const close = () => call(() => post(`/paper/sessions/${sid}/close`), () => { setSid(null); setSt(null); list.reload(); });
  const tick = () => call(() => post("/paper/scheduler/tick"), () => { sched.reload(); setSt(null); });
  const adv = (n: number) => call(() => post(`/paper/sessions/${sid}/advance`, { steps: n }), setSt);
  const order = () => call(() => post(`/paper/sessions/${sid}/orders`, { symbol: o.symbol, qty: Number(o.qty), type: o.type, limit: o.limit ? Number(o.limit) : null }), (r) => setMsg(`${r.status}${r.reason ? `: ${r.reason}` : r.note ? ` — ${r.note}` : ""}`));
  const lib = (strategies.data?.library || []).filter((x: any) => x.intervals.includes("1d") && x.requires === "EQ");
  return (
    <Page title="Paper Trading" sub="Simulated orders only — nothing is sent to a broker. The session uses the same matching engine, costs, margin and ledger as the backtester, fed one bar at a time.">
      <AccountKinds here="paper" />
      <Banner tone="info"><b>Two feeds.</b> <i>Replay</i> steps through stored bars — “Advance” plays the role of the clock. <i>Kite daily</i> / <i>Angel One daily</i> pull each day’s completed candle after the close and step once per trading day (mock-tested; not yet verified against the real services). Live 1-minute data is on the Strategy Monitor; no streaming tick feed exists. Sessions are saved to disk and come back after a restart.</Banner>
      {sched.data && <Card title="Scheduler" right={<button onClick={tick} disabled={busy}>Run one pass now</button>}>
        <KV items={[["timer", <Badge key="t" s={sched.data.running ? "running" : "off (set NSEQL_SCHEDULER=true)"} tone={sched.data.running ? "good" : "warn"} />], ["last pass", sched.data.last_tick || "never"],
          ["holiday calendar", sched.data.holiday_calendar], ["errors", Object.entries(sched.data.errors).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none"],
          ["not restored after restart", Object.entries(sched.data.restore_errors).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none"]]} />
      </Card>}
      <div style={{ height: 14 }} />
      <Err e={err} />
      <Card title="Start a session">
        <div className="row">
          <label>Algo strategy<select value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}><option value="">none (manual orders only)</option>{lib.map((x: any) => <option key={x.name}>{x.name}</option>)}</select></label>
          <label>Feed<select value={f.feed} onChange={(e) => setF({ ...f, feed: e.target.value })}><option value="replay">replay (stored bars)</option><option value="kite_daily">Kite daily (end of day)</option><option value="angel_daily">Angel One daily (end of day)</option></select></label>
          {f.feed === "kite_daily" && <label style={{ flex: 1, minWidth: 220 }}>NSE symbols (comma or space separated)<input value={f.symbols} onChange={(e) => setF({ ...f, symbols: e.target.value.toUpperCase() })} placeholder="RELIANCE, TCS, INFY" /></label>}
          {f.feed === "replay" && <label>Auto-advance (bars per scheduler pass)<input type="number" min={0} max={50} value={f.auto_bars} onChange={(e) => setF({ ...f, auto_bars: Number(e.target.value) })} style={{ width: 90 }} /></label>}
          <label>Trade from<select value={f.window} disabled={f.feed !== "replay"} onChange={(e) => setF({ ...f, window: e.target.value })}><option value="sealed">start of sealed window (forward test)</option><option value="validation">start of validation window</option><option value="all">first bar</option></select></label>
          <label>Algo capital (₹)<input type="number" value={f.capital} onChange={(e) => setF({ ...f, capital: Number(e.target.value) })} style={{ width: 130 }} /></label>
          <button className="primary" onClick={start} disabled={busy}>Start session</button>
          {(list.data?.length || 0) > 0 && <label>Open existing<select value={sid || ""} onChange={(e) => { setSid(e.target.value); setSt(null); }}><option value="">—</option>{list.data!.map((x) => <option key={x.session_id} value={x.session_id}>{x.session_id} · {x.label}</option>)}</select></label>}
        </div>
      </Card>
      {s && <>
        <div className="top" style={{ marginTop: 16 }}><h1>{s.label} <span className="mono dim">{s.session_id || sid}</span></h1><Badge s={s.halted ? "HALTED" : "running"} tone={s.halted ? "crit" : "good"} /><span className="sp" />
          <Badge s="saved to disk" tone="good" /><button onClick={close} disabled={busy}>Close session</button><button onClick={() => adv(1)} disabled={busy || !s.remaining}>+1 bar</button><button onClick={() => adv(5)} disabled={busy || !s.remaining}>+5</button><button onClick={() => adv(20)} disabled={busy || !s.remaining}>+20</button></div>
        <Synthetic on={s.synthetic} />
        <DataSource d={s.data_source} />
        {s.kill_switch?.reason?.startsWith("WARMUP") && <Banner tone="info">Loading history: no orders are accepted until the first live day.</Banner>}
        {s.halted && <Banner tone="crit"><b>Session halted:</b> {s.halted}. Working orders were cancelled; positions are untouched. New orders are refused until a named human resets the kill switch and a new session is started.</Banner>}
        <div className="stats">
          <Stat k="Session clock" v={String(s.now).slice(0, 10)} h={s.remaining != null ? `${s.remaining} bars left in replay` : (s.feed_status || "end-of-day feed")} /><Stat k="Equity" v={inr(s.equity)} h={`capital ${inr(s.capital)}`} tone={s.equity >= s.capital ? "up" : "down"} />
          <Stat k="Cash" v={inr(s.cash)} /><Stat k="Sharpe so far" v={num(s.metrics?.sharpe)} /><Stat k="Max drawdown" v={pct(s.metrics?.max_drawdown)} /><Stat k="Positions" v={s.positions.length} h={`${s.open_orders.length} working orders`} />
          <Stat k="Reconciliation" v={s.reconciliation?.ok ? "matched" : "BREAK"} h={`${s.reconciliation?.fills ?? 0} fills vs ledger`} tone={s.reconciliation?.ok ? "up" : "down"} />
        </div>
        <div className="grid g21" style={{ marginTop: 14 }}>
          <Card title="Equity">{s.curve.ts.length > 1 ? <LineChart x={s.curve.ts} series={[{ name: "equity", values: s.curve.equity }]} fmt={(v) => inr(v)} /> : <div className="empty">Advance the session to build a curve.</div>}</Card>
          <Card title="Order ticket (manual book)">
            <div className="row">
              <label>Symbol<input value={o.symbol} onChange={(e) => setO({ ...o, symbol: e.target.value.toUpperCase() })} style={{ width: 130 }} /></label>
              <label>Qty (negative = sell)<input type="number" value={o.qty} onChange={(e) => setO({ ...o, qty: Number(e.target.value) })} style={{ width: 100 }} /></label>
              <label>Type<select value={o.type} onChange={(e) => setO({ ...o, type: e.target.value })}><option>MARKET</option><option>LIMIT</option></select></label>
              {o.type === "LIMIT" && <label>Limit<input value={o.limit} onChange={(e) => setO({ ...o, limit: e.target.value })} style={{ width: 100 }} /></label>}
              <button className="primary" onClick={order} disabled={busy || !!s.halted}>Place paper order</button>
            </div>
            {msg && <p style={{ marginTop: 8 }}>{msg}</p>}
            <p className="dim" style={{ marginTop: 8 }}>An order is submitted at the next bar and can first fill on the bar after that — exactly as in a backtest. Delivery sells need holdings.</p>
            <h3>Independent position check</h3>
            <KV items={[["ledger identities", <Badge key="l" s={s.reconciliation?.ledger_checks?.ok ? "pass" : "fail"} />], ["positions vs fill tape", s.reconciliation?.position_breaks?.length ? `${s.reconciliation.position_breaks.length} breaks` : "no breaks"]]} />
          </Card>
        </div>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="Positions"><DataTable maxHeight={260} cols={[{ key: "book" }, { key: "symbol" }, { key: "qty", r: true }, { key: "avg_price", label: "avg", r: true, f: (r) => num(r.avg_price) }, { key: "mark", r: true, f: (r) => num(r.mark) },
            { key: "unrealized", label: "unrealised", r: true, f: (r) => <span className={r.unrealized >= 0 ? "up" : "down"}>{num(r.unrealized)}</span> }]} rows={s.positions} empty="Flat." /></Card>
          <Card title="Orders (latest first)"><DataTable maxHeight={260} cols={[{ key: "id", r: true }, { key: "book" }, { key: "symbol" }, { key: "side", f: (r) => (r.side > 0 ? "BUY" : "SELL") }, { key: "qty", r: true }, { key: "type" },
            { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "reason", f: (r) => r.reason || "" }]} rows={[...s.orders].reverse()} empty="No orders yet." /></Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Fills (latest first)"><DataTable maxHeight={240} cols={[{ key: "ts", f: (r) => r.ts.slice(0, 16) }, { key: "book" }, { key: "symbol" }, { key: "side", f: (r) => (r.side > 0 ? "BUY" : "SELL") }, { key: "qty", r: true },
          { key: "price", r: true, f: (r) => num(r.price) }, { key: "charges", r: true, f: (r) => num(r.charges) }, { key: "reason" }]} rows={[...s.fills].reverse()} empty="No fills yet." /></Card></div>
      </>}
    </Page>
  );
}
