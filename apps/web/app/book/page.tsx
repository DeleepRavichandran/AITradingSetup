"use client";
/* Paper Portfolio: positions, cash and marked equity reconciled, and the account next to its benchmarks.
   Data: /today (account, holdings), /radar (limits), /benchmarks. Simulated positions only. */
import { Donut } from "@/components/charts";
import { Tag, Usage, signedInr, signedPct } from "@/components/vega";
import { inr, pct, useApi } from "@/lib/api";

export default function Book() {
  const t = useApi<any>("/today", 15000);
  const rd = useApi<any>("/radar", 30000);
  const bm = useApi<any>("/benchmarks", 300000);
  const d = t.data, a = d?.account, h: any[] = d?.holdings || [];
  const unreal = h.reduce((x, r) => x + (r.unrealised_inr || 0), 0);
  const held = h.reduce((x, r) => x + (r.value_inr || 0), 0);
  const snap = rd.data?.limits?.snapshot;
  const reserved = snap ? snap.reserved_inr : null;
  const gap = a && snap ? a.value_now - (a.cash_free + (reserved || 0) + held) : null;
  const ser = bm.data?.series || {};
  return (
    <>
      <div className="vhead"><div><h1>Paper Portfolio</h1><p>Positions, cash and marked equity reconcile. Every rupee is simulated.</p></div><Tag tone="info">Simulated</Tag></div>
      <div className="grid g21">
        <section className="vp" aria-labelledby="pos-h">
          <div className="h"><div><h2 id="pos-h">Open positions</h2><div className="sub2">Marked at the last 1-minute close; after entry charges, before exit charges</div></div></div>
          {h.length ? <div className="tw"><table data-testid="positions">
            <thead><tr><th>Stock</th><th>Strategy</th><th className="r">Qty</th><th className="r">Entry</th><th className="r">Mark</th><th className="r">Stop</th><th className="r">Value</th><th className="r">Unrealised</th></tr></thead>
            <tbody>{h.map((r) => <tr key={r.config_id}><td><b>{r.symbol}</b></td><td>{r.rule} <span className="dim">{r.timeframe}</span></td><td className="r">{r.qty}</td>
              <td className="r">{r.entry_price}</td><td className="r">{r.last_price}</td><td className="r">{r.stop_price}</td><td className="r">{inr(r.value_inr)}</td>
              <td className="r"><span className={r.unrealised_inr >= 0 ? "up" : "down"}>{signedInr(r.unrealised_inr, 2)}</span></td></tr>)}</tbody>
          </table></div> : <p className="empty">Nothing held right now. Holding cash is a valid decision.</p>}
        </section>
        <section className="vp" aria-labelledby="rec-h">
          <div className="h"><div><h2 id="rec-h">Capital reconciliation</h2><div className="sub2">{d?.charts?.session || ""}</div></div>
            {gap != null && <Tag tone={Math.abs(gap) < 1 ? "ok" : "wait"}>{Math.abs(gap) < 1 ? "Reconciles" : `Gap ${inr(gap, 2)}`}</Tag>}</div>
          <div className="b vlist" data-testid="reconciliation">
            <div><span>Equity at the session open</span><b>{a ? inr(a.day_open_equity, 2) : "—"}</b></div>
            <div><span>Realised today (net of charges)</span><b className={a && a.today_realised >= 0 ? "up" : "down"}>{a ? signedInr(a.today_realised, 2) : "—"}</b></div>
            <div><span>Charges paid today</span><b>{a ? inr(a.today_charges, 2) : "—"}</b></div>
            <div><span>Unrealised on open positions</span><b className={unreal >= 0 ? "up" : "down"}>{signedInr(unreal, 2)}</b></div>
            <div><span>Marked equity now</span><b>{a ? inr(a.value_now, 2) : "—"}</b></div>
            <div><span>Free cash + reserved + holdings</span><b>{a ? `${inr(a.cash_free)} + ${reserved == null ? "—" : inr(reserved)} + ${inr(held)}` : "—"}</b></div>
          </div>
        </section>
      </div>
      <div className="grid g2" style={{ marginTop: 14 }}>
        <section className="vp" aria-labelledby="alloc-h">
          <div className="h"><div><h2 id="alloc-h">Where the money is</h2></div></div>
          <div className="b">{d?.charts?.allocation?.length ? <Donut items={d.charts.allocation} fmt={(v) => inr(v)} label="Allocation of the paper account" centre={a ? inr(a.value_now) : ""} /> : <p className="empty">—</p>}</div>
        </section>
        <section className="vp" aria-labelledby="bm-h">
          <div className="h"><div><h2 id="bm-h">Versus simpler choices</h2><div className="sub2">Same sessions, after costs where costs apply</div></div></div>
          {Object.keys(ser).length ? <div className="tw"><table data-testid="benchmarks">
            <thead><tr><th>Series</th><th className="r">Return</th><th className="r">Max drawdown</th><th className="r">Sharpe</th><th className="r">Worst day</th></tr></thead>
            <tbody>{Object.entries(ser).map(([k, v]: any) => <tr key={k}><td>{k}</td><td className="r"><span className={v.return >= 0 ? "up" : "down"}>{signedPct(v.return)}</span></td>
              <td className="r">{pct(v.max_drawdown, 2)}</td><td className="r">{v.sharpe ?? "—"}</td><td className="r">{v.worst_day == null ? "—" : signedPct(v.worst_day)}</td></tr>)}</tbody>
          </table></div> : <p className="empty">{bm.error ? `Benchmarks unavailable: ${bm.error}` : "Loading…"}</p>}
          <div className="ft"><span>{bm.data?.plain}</span><span>{(bm.data?.notes || [])[0]}</span></div>
        </section>
      </div>
      <section className="vp" style={{ marginTop: 14 }} aria-labelledby="lim-h">
        <div className="h"><div><h2 id="lim-h">Limits in use</h2></div></div>
        <div className="b"><Usage usage={rd.data?.limits?.usage} /></div>
      </section>
    </>
  );
}
