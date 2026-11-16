"use client";
import { useEffect, useState } from "react";
import { Candles } from "@/components/charts";
import { Badge, Card, DataTable, Err, KV, Page, Synthetic } from "@/components/ui";
import { num, useApi, useQuery } from "@/lib/api";

export default function Replay() {
  const q = useQuery();
  const runs = useApi<any[]>("/runs?limit=60");
  const [run, setRun] = useState<string | null>(null);
  const [i, setI] = useState(0);
  useEffect(() => { if (q.get("run")) { setRun(q.get("run")); setI(Number(q.get("trade") || 0)); } }, [q]);
  const withFiles = (runs.data || []).filter((r) => r.path && (r.metrics?.trades || 0) > 0);
  const rid = run || withFiles[0]?.run_id || null;
  const trades = useApi<any>(rid ? `/runs/${rid}/trades` : null);
  const rp = useApi<any>(rid && trades.data?.total ? `/replay/${rid}/${Math.min(i, trades.data.total - 1)}` : null);
  const d = rp.data, t = d?.trade;
  return (
    <Page title="Trade Replay" sub="One trade at a time: the bars around it, the order that was placed, the fills it received, the engine events and the exact cash-ledger lines. Markers sit on the bar where each fill happened."
      right={<select value={rid || ""} onChange={(e) => { setRun(e.target.value); setI(0); }}>{withFiles.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.strategy} · {r.metrics.trades} trades</option>)}</select>}>
      <Err e={trades.error || rp.error} />
      {!rid && <div className="empty">No runs with trades yet. Run a backtest first.</div>}
      {d && t && <>
        <Synthetic on={d.synthetic} />
        <div className="row" style={{ marginBottom: 10 }}>
          <button onClick={() => setI(Math.max(i - 1, 0))} disabled={i <= 0}>← previous</button>
          <b>Trade {d.index + 1} of {d.total_trades}</b>
          <button onClick={() => setI(Math.min(i + 1, d.total_trades - 1))} disabled={i >= d.total_trades - 1}>next →</button>
          <Badge s={t.net > 0 ? "winner" : "loser"} tone={t.net > 0 ? "good" : "crit"} />
        </div>
        <div className="grid g21">
          <Card title={`${t.symbol} · ${t.direction > 0 ? "long" : "short"} ${t.qty}`}>
            <Candles ts={d.bars.ts} open={d.bars.open} high={d.bars.high} low={d.bars.low} close={d.bars.close}
              markers={d.fills.map((f: any) => ({ ts: f.ts, price: f.price, side: f.side, label: `${f.side > 0 ? "B" : "S"} ${num(f.price)}` }))} />
            <p className="dim">{d.signal_note}.</p>
          </Card>
          <Card title="Result">
            <KV items={[["entry", `${t.entry_ts.slice(0, 16)} @ ${num(t.entry_price)}`], ["exit", `${t.exit_ts.slice(0, 16)} @ ${num(t.exit_price)}`], ["held", `${num(t.holding_days, 1)} days`],
              ["gross P&L", num(t.gross)], ["charges", num(t.charges)], ["net P&L", <b key="n" className={t.net > 0 ? "up" : "down"}>{num(t.net)}</b>], ["exit reason", t.exit_reason], ["tag", t.tag || "—"]]} />
            <h3>Orders</h3>
            <DataTable maxHeight={150} cols={[{ key: "id", r: true }, { key: "type" }, { key: "side", f: (r) => (r.side > 0 ? "BUY" : "SELL") }, { key: "qty", r: true }, { key: "submitted_ts", label: "submitted", f: (r) => r.submitted_ts.slice(0, 16) },
              { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "avg_fill_price", label: "avg fill", r: true, f: (r) => num(r.avg_fill_price) }]} rows={d.orders} empty="Engine-generated (expiry / square-off)." />
          </Card>
        </div>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="Fills and charges">
            <DataTable maxHeight={220} cols={[{ key: "ts", f: (r) => r.ts.slice(0, 16) }, { key: "side", f: (r) => (r.side > 0 ? "BUY" : "SELL") }, { key: "qty", r: true }, { key: "price", r: true, f: (r) => num(r.price) },
              { key: "charges", r: true, f: (r) => num(r.charges) }, { key: "detail", f: (r) => <span className="mono">{Object.entries(r.charges_detail).filter(([k, v]) => k !== "total" && v).map(([k, v]: any) => `${k} ${(v / 100).toFixed(2)}`).join(" ")}</span> }, { key: "reason" }]} rows={d.fills} />
          </Card>
          <Card title="Cash ledger lines">
            <DataTable maxHeight={220} cols={[{ key: "seq", r: true }, { key: "ts", f: (r) => String(r.ts).slice(0, 16) }, { key: "type" }, { key: "amount", r: true, f: (r) => num(r.amount) }, { key: "cash_after", label: "cash after", r: true, f: (r) => num(r.cash_after) }, { key: "memo" }]} rows={d.ledger} />
          </Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Engine events for this symbol during the trade">
          <DataTable maxHeight={240} cols={[{ key: "seq", r: true }, { key: "ts", f: (r) => r.ts.slice(0, 16) }, { key: "type" }, { key: "detail", f: (r) => <span className="mono">{JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !["seq", "ts", "type"].includes(k)))).slice(0, 160)}</span> }]} rows={d.events} />
        </Card></div>
      </>}
      {trades.data && <div style={{ marginTop: 14 }}><Card title="All trades in this run">
        <DataTable maxHeight={300} onRow={(_, k) => setI(k)} selected={(_, k) => k === i} cols={[{ key: "n", label: "#", f: (_, k) => k + 1 }, { key: "symbol" }, { key: "entry_ts", label: "entry", f: (r) => r.entry_ts.slice(0, 10) },
          { key: "exit_ts", label: "exit", f: (r) => r.exit_ts.slice(0, 10) }, { key: "qty", r: true }, { key: "net", r: true, f: (r) => <span className={r.net > 0 ? "up" : "down"}>{num(r.net)}</span> }, { key: "exit_reason", label: "exit reason" }]} rows={trades.data.items} />
      </Card></div>}
    </Page>
  );
}
