"use client";
import { useState } from "react";
import { Bars, Candles, Heatmap } from "@/components/charts";
import { Badge, Card, DataTable, Err, KV, Loading, Page, Signed, Stat, Synthetic } from "@/components/ui";
import { inr, num, pct, useApi } from "@/lib/api";

export default function Market() {
  const [ds, setDs] = useState("synthetic_v1");
  const dss = useApi<any[]>("/datasets");
  const ov = useApi<any>(`/market/overview?dataset_id=${ds}`);
  const [sym, setSym] = useState<string | null>(null);
  const symbol = sym || ov.data?.symbols?.[0]?.symbol || null;
  const bars = useApi<any>(symbol ? `/datasets/${ds}/bars?symbol=${encodeURIComponent(symbol)}&limit=180${ov.data ? `&end=${ov.data.as_of}` : ""}` : null);
  const idx = ov.data?.symbols?.find((s: any) => s.kind === "INDEX")?.symbol;
  const chain = useApi<any>(idx && ov.data ? `/datasets/${ds}/chain?underlying=${idx}&date=${ov.data.as_of}` : null);
  const [exp, setExp] = useState<string | null>(null);
  const structure = useApi<any[]>("/market/structure");
  const o = ov.data, b = bars.data, br = o?.brief;
  const expiry = exp || chain.data?.expiries?.[0];
  return (
    <Page title="Market Intelligence" sub="The loaded universe as the research team sees it: only data up to the end of the validation window is shown, so nothing here can leak the sealed period."
      right={<select aria-label="Dataset" value={ds} onChange={(e) => { setDs(e.target.value); setSym(null); }}>{(dss.data || []).map((d) => <option key={d.dataset_id}>{d.dataset_id}</option>)}</select>}>
      <Err e={ov.error} /><Loading on={ov.loading} />
      {o && <>
        <Synthetic on={o.synthetic} />
        <div className="stats" style={{ marginBottom: 14 }}>
          <Stat k="As of" v={o.as_of} h="end of validation window" />
          <Stat k="Breadth" v={`${o.breadth.above_50d}/${o.breadth.total}`} h="above 50-day average" />
          <Stat k="Median volatility" v={pct(br.median_annual_vol)} h="annualised, development window" />
          <Stat k="Avg pair correlation" v={num(br.avg_pairwise_correlation)} />
          <Stat k="Momentum rank-IC" v={num(br.momentum_score, 3)} h="120d return → next 21d" />
          <Stat k="Reversal rank-IC" v={num(br.reversal_score, 3)} h="3d return → next 3d" />
        </div>
        <div className="grid g21">
          <Card title={symbol ? `${symbol} · daily` : "Price"}>
            {b && <Candles ts={b.ts} open={b.open} high={b.high} low={b.low} close={b.close} />}
            {b && <KV items={[["sector", b.instrument.sector || "—"], ["lot size", b.instrument.lot_size], ["tick", b.instrument.tick_size], ["price band", b.instrument.price_band_pct ? pct(b.instrument.price_band_pct, 0) : "none"],
              ["listed", `${b.instrument.listed_from || "?"} → ${b.instrument.listed_to || "present"}`]]} />}
          </Card>
          <Card title="Sector return over the development window"><Bars items={Object.entries(br.sector_returns).map(([label, value]) => ({ label, value: value as number }))} fmt={(v) => pct(v, 0)} /></Card>
        </div>
        <div style={{ marginTop: 14 }}><Card title="Universe (click a row to chart it)">
          <DataTable onRow={(r) => setSym(r.symbol)} selected={(r) => r.symbol === symbol} maxHeight={330} cols={[
            { key: "symbol" }, { key: "kind" }, { key: "sector", f: (r) => r.sector || "—" }, { key: "last", r: true, f: (r) => num(r.last) },
            { key: "ret_1m", label: "1m", r: true, f: (r) => <Signed v={r.ret_1m} f={(x) => pct(x)} /> }, { key: "ret_3m", label: "3m", r: true, f: (r) => <Signed v={r.ret_3m} f={(x) => pct(x)} /> },
            { key: "vol_20d", label: "vol 20d", r: true, f: (r) => pct(r.vol_20d) }, { key: "traded_value", label: "median traded value", r: true, f: (r) => (r.traded_value ? inr(r.traded_value) : "—") },
            { key: "above_50d", label: "trend", f: (r) => (r.above_50d ? "above 50d" : "below 50d") }, { key: "listed_to", label: "delisted", f: (r) => r.listed_to || "" }]} rows={o.symbols} />
        </Card></div>
        <div className="grid g2" style={{ marginTop: 14 }}>
          <Card title="Return correlation, last 120 sessions"><Heatmap labels={o.correlation.labels} matrix={o.correlation.matrix} /></Card>
          <Card title={`Option chain · ${idx || "no index"} · ${chain.data?.date || ""}`} right={chain.data && <select aria-label="Expiry" value={expiry} onChange={(e) => setExp(e.target.value)}>{chain.data.expiries.map((e: string) => <option key={e}>{e}</option>)}</select>}>
            {chain.data ? <><p className="dim">Spot {num(chain.data.spot)}. {chain.data.greeks_note}.</p>
              <DataTable maxHeight={330} cols={[{ key: "strike", r: true }, { key: "type" }, { key: "close", r: true, f: (r) => num(r.close) }, { key: "iv", label: "IV", r: true, f: (r) => pct(r.iv) },
                { key: "delta", r: true, f: (r) => num(r.delta) }, { key: "theta", r: true, f: (r) => num(r.theta) }, { key: "vega", r: true, f: (r) => num(r.vega) },
                { key: "volume", r: true, f: (r) => num(r.volume, 0) }, { key: "oi", label: "OI", r: true, f: (r) => num(r.oi, 0) }]} rows={chain.data.rows.filter((r: any) => r.expiry === expiry)} /></> : <div className="empty">{chain.error || "No option data for this dataset."}</div>}
          </Card>
        </div>
      </>}
      <div style={{ marginTop: 14 }}><Card title="Market-structure timeline (reference; unverified dates are marked)">
        <DataTable wrap maxHeight={360} cols={[{ key: "date" }, { key: "category" }, { key: "title" }, { key: "effect", label: "what it means for a backtest" },
          { key: "verified", f: (r) => <Badge s={r.verified ? "verified" : "unverified"} tone={r.verified ? "good" : "warn"} /> }, { key: "source" }]} rows={structure.data} />
      </Card></div>
    </Page>
  );
}
