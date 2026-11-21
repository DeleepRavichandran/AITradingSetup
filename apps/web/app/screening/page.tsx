"use client";
import { useState } from "react";
import { Badge, Banner, Card, DataSource, DataTable, Err, KV, Loading, Page, Stat } from "@/components/ui";
import { inr, num, post, useApi, useJob } from "@/lib/api";

const HORIZONS = [["", "All"], ["intraday", "Intraday"], ["swing", "Swing"]] as const;
const PAGE = 50;

function Pager({ total, offset, set }: { total: number; offset: number; set: (o: number) => void }) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
      <button disabled={offset === 0} onClick={() => set(Math.max(0, offset - PAGE))}>Previous</button>
      <span className="dim">{total ? `${offset + 1}–${Math.min(offset + PAGE, total)} of ${num(total, 0)}` : "none"}</span>
      <button disabled={offset + PAGE >= total} onClick={() => set(offset + PAGE)}>Next</button>
    </div>
  );
}

export default function Screening() {
  const [hz, setHz] = useState<string>("");
  const [sigOff, setSigOff] = useState(0);
  const [cfgOff, setCfgOff] = useState(0);
  const [decision, setDecision] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, running } = useJob(jobId);
  const status = useApi<any>("/screening/status", 10000);
  const started = status.data?.started;
  const summary = useApi<any>(started ? "/screening/summary" : null, 10000);
  const signals = useApi<any>(started ? `/screening/signals?horizon=${hz}&decision=${decision}&limit=${PAGE}&offset=${sigOff}` : null, 10000);
  const configs = useApi<any>(started ? `/screening/configs?horizon=${hz}&limit=${PAGE}&offset=${cfgOff}` : null);
  const orders = useApi<any>(started ? `/screening/orders?horizon=${hz}&status=filled&limit=${PAGE}` : null, 10000);
  const cycles = useApi<any>(started ? "/screening/cycles?limit=30" : null, 10000);
  const pf = useApi<any>(started ? "/screening/portfolio" : null, 10000);
  const s = status.data, sm = summary.data;
  const replay = async () => {
    setErr(null);
    try { setJobId((await post("/screening/replay", { sessions: 5 })).id); } catch (e: any) { setErr(e.message); }
  };
  const tab = (h: string) => { setHz(h); setSigOff(0); setCfgOff(0); };
  const hzRows = sm ? Object.entries(sm.horizons).filter(([k]) => !hz || k === hz) : [];
  return (
    <Page title="Strategy Monitor" sub="The one-minute screening loop: registered configurations, which were evaluated, the decisions they made and the forward paper ledger. Simulated orders only."
      right={<button className="primary" disabled={running} onClick={replay}>{running ? "Replay running…" : "Run 5-session replay"}</button>}>
      <Err e={status.error || err} /><Loading on={status.loading} />
      {job && <Banner tone="info">Replay job <span className="mono">{job.id}</span>: <Badge s={job.status} /> {job.progress?.length ? `minute ${job.progress[job.progress.length - 1].minute}/${job.progress[job.progress.length - 1].of}` : ""} {job.error}</Banner>}
      {s && !started && <Banner>No screening run yet. Run a replay of stored 1-minute bars, or set up Angel One for live 1-minute data (the Angel One adapter is mock-tested and not yet verified against the real service).{s.live?.state === "error" ? ` Live start failed: ${s.live.error}` : ""}</Banner>}
      {started && <>
        <DataSource d={s.data_source} />
        <Banner tone="info">Live trading is off; every order on this page is simulated. Feed: {s.feed}{s.feed_status ? ` (${s.feed_status})` : ""}.</Banner>
        <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
          {HORIZONS.map(([k, label]) => <button key={k} className={hz === k ? "primary" : ""} onClick={() => tab(k)}>{label}</button>)}
        </div>
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="Registered" v={num(s.registered, 0)} h={`${s.symbols} symbols`} />
          {hzRows.map(([k, v]: any) => <Stat key={k} k={`${k}: evaluated last minute`} v={num(v.evaluated_last_minute, 0)} h={`${num(v.evaluated_ever, 0)} evaluated at least once`} />)}
          {hzRows.map(([k, v]: any) => <Stat key={`p${k}`} k={`${k}: open paper positions`} v={num(v.open_positions, 0)} h={`${v.pending_orders} pending`} />)}
          <Stat k="Last minute processed" v={s.watermark ? s.watermark.slice(5, 16).replace("T", " ") : "—"} h={sm?.latency_ms_recent ? `p99 ${num(sm.latency_ms_recent.p99, 1)} ms` : ""} />
        </div>
        {pf.data && <Card title={pf.data.shared_capital ? "Shared portfolio (one capital pot)" : "Execution"}>
          {pf.data.halted_today && <Banner tone="crit">Daily loss limit reached: no new entries for the rest of the day. Exits continue.</Banner>}
          <div className="stats" style={{ marginBottom: 10 }}>
            <Stat k="Equity" v={inr(pf.data.equity_inr)} h={pf.data.shared_capital ? `day start ${inr(pf.data.day_start_equity_inr)}` : ""} />
            <Stat k="Free cash" v={inr(pf.data.cash_inr - pf.data.reserved_inr)} h={`${inr(pf.data.reserved_inr)} reserved for working orders`} />
            <Stat k="Open positions" v={num(pf.data.open_positions, 0)} h={pf.data.rules ? `limit ${pf.data.rules.max_positions}` : ""} />
            <Stat k="Gross exposure" v={inr(pf.data.gross_exposure_inr)} h={`${pf.data.working_orders} working orders`} />
          </div>
          <KV items={[
            ["Execution", pf.data.execution ? `realistic: ≤${(pf.data.execution.participation * 100).toFixed(0)}% of each bar's volume, ${pf.data.execution.half_spread_bps} bps half-spread + impact, entries time out after ${pf.data.execution.entry_ttl_minutes} min` : "simple: whole order at the next open"],
            ["Limits", pf.data.rules ? `${(pf.data.rules.per_trade_pct * 100).toFixed(0)}% of equity per trade · ≤${(pf.data.rules.max_symbol_pct * 100).toFixed(0)}% per symbol · ≤${(pf.data.rules.max_gross_pct * 100).toFixed(0)}% gross · stop new entries at −${(pf.data.rules.daily_loss_pct * 100).toFixed(0)}% on the day` : "none (each configuration has its own Rs 1,00,000)"],
            ["Signals refused", Object.keys(pf.data.refused_signals || {}).length ? Object.entries(pf.data.refused_signals).map(([k, v]: any) => `${k}: ${num(v, 0)}`).join(" · ") : "none"],
            ["Largest holdings", (pf.data.top_symbols || []).map((x: any) => `${x.symbol} ${(x.pct_equity * 100).toFixed(1)}%`).join(" · ") || "none"],
          ] as [string, React.ReactNode][]} />
          <p className="dim" style={{ marginTop: 6 }}>{pf.data.note}</p>
        </Card>}
        <Card title="Forward paper ledger (simulated)">
          <KV items={Object.entries(sm?.paper || {}).filter(([k]) => !hz || k === hz).map(([k, v]: any) =>
            [k, `${num(v.filled, 0)} fills · realised ${inr(v.realized_inr)} after charges ${inr(v.charges_inr)}`] as [string, React.ReactNode])} />
          <p className="dim" style={{ marginTop: 6 }}>{sm?.note}</p>
        </Card>
        <Card title="Decisions" right={<select aria-label="Filter decisions" value={decision} onChange={(e) => { setDecision(e.target.value); setSigOff(0); }}>
          <option value="">BUY + EXIT</option><option value="BUY">BUY</option><option value="EXIT">EXIT</option></select>}>
          <Err e={signals.error} />
          <DataTable maxHeight={420} rows={signals.data?.rows || []} cols={[
            { key: "bar_ts", label: "bar close", f: (r: any) => r.bar_ts.replace("T", " ") }, { key: "symbol", label: "symbol" },
            { key: "horizon", label: "horizon" }, { key: "decision", label: "decision", f: (r: any) => <Badge s={r.decision} tone={r.decision === "BUY" ? "good" : "warn"} /> },
            { key: "reason", label: "why" }, { key: "price", label: "close", r: true, f: (r: any) => num(r.price, 2) },
            { key: "config_id", label: "configuration", f: (r: any) => <span className="mono">{r.config_id}</span> }]} />
          <Pager total={signals.data?.total || 0} offset={sigOff} set={setSigOff} />
        </Card>
        <Card title="Recent cycles">
          <DataTable maxHeight={300} rows={cycles.data?.rows || []} cols={[
            { key: "minute", label: "minute", f: (r: any) => r.minute.replace("T", " ") }, { key: "status", label: "status", f: (r: any) => <Badge s={r.status} tone={r.status === "processed" ? "good" : "crit"} /> },
            { key: "symbols_with_data", label: "symbols with data", r: true, f: (r: any) => `${r.symbols_with_data}/${r.symbols_expected}` },
            { key: "closed", label: "bars closed", f: (r: any) => (r.closed || []).join(" ") }, { key: "eligible", label: "eligible", r: true },
            { key: "evaluated", label: "evaluated", r: true }, { key: "latency_ms", label: "ms", r: true, f: (r: any) => num(r.latency_ms, 1) }]} />
        </Card>
        <Card title="Filled paper orders">
          <DataTable maxHeight={300} rows={orders.data?.rows || []} cols={[
            { key: "fill_ts", label: "filled", f: (r: any) => (r.fill_ts || "").replace("T", " ") }, { key: "symbol", label: "symbol" }, { key: "side", label: "side" },
            { key: "product", label: "product" }, { key: "qty", label: "qty", r: true }, { key: "price", label: "price", r: true, f: (r: any) => num(r.price, 2) },
            { key: "charges_paise", label: "charges", r: true, f: (r: any) => inr(r.charges_paise / 100, 2) },
            { key: "realized_paise", label: "realised", r: true, f: (r: any) => (r.side === "SELL" ? inr(r.realized_paise / 100, 0) : "") }]} />
        </Card>
        <Card title="Registered configurations">
          <DataTable maxHeight={300} rows={configs.data?.rows || []} cols={[
            { key: "id", label: "id", f: (r: any) => <span className="mono">{r.id}</span> }, { key: "symbol", label: "symbol" }, { key: "timeframe", label: "timeframe" },
            { key: "horizon", label: "horizon" }, { key: "family", label: "family" }, { key: "params", label: "parameters", f: (r: any) => JSON.stringify(r.params) },
            { key: "stop_pct", label: "stop", r: true, f: (r: any) => `${(r.stop_pct * 100).toFixed(1)}%` }]} />
          <Pager total={configs.data?.total || 0} offset={cfgOff} set={setCfgOff} />
        </Card>
      </>}
    </Page>
  );
}
