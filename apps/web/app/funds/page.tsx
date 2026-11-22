"use client";
/* Mutual Funds: daily NAVs imported from AMFI files (/api/funds/*). No intraday prices exist for funds and
   none are shown. With no imported NAVs the page says so; it never shows sample funds. */
import { useEffect, useState } from "react";
import { LineChart } from "@/components/charts";
import { Banner, Col, Err, KV, Metric, Panel, Prov, Skeleton, Unavailable, VirtualTable } from "@/components/ui";
import { api, inr, num, pct, post, useApi } from "@/lib/api";

function Scheme({ code }: { code: string }) {
  const d = useApi<any>(`/funds/schemes/${code}`);
  const [sip, setSip] = useState({ monthly: 10_000, start: "", end: "", step_up: 0 });
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const x = d.data;
  useEffect(() => { if (x?.provenance?.period) setSip((s) => ({ ...s, start: x.provenance.period[0], end: x.provenance.period[1] })); }, [x]);
  const run = async () => {
    setErr(null);
    try { setRes(await api(`/funds/schemes/${code}/sip?${new URLSearchParams({ monthly: String(sip.monthly), start: sip.start, end: sip.end, step_up: String(sip.step_up / 100) })}`)); }
    catch (e: any) { setErr(e.message); setRes(null); }
  };
  if (d.loading) return <Skeleton lines={5} />;
  if (!x) return <Err e={d.error} />;
  const a = x.analytics;
  return (
    <div className="stack" data-testid="scheme">
      <Panel title={x.scheme.name} hint={`${x.scheme.category || "category unknown"} · ${x.scheme.amc || ""}`} id="scheme"
        prov={<Prov source={x.provenance.source} period={x.provenance.period} note={`${x.provenance.points} NAVs`} />}>
        <div className="metrics">
          <Metric k="Latest NAV" v={a.last_nav != null ? num(a.last_nav, 4) : "—"} h={a.last} />
          {Object.entries(a.cagr || {}).map(([k, v]: any) => <Metric key={k} sm k={`CAGR ${k}`} v={v == null ? "—" : pct(v, 2)} h={v == null ? "series too short" : "point to point"} />)}
          <Metric sm k="Volatility" v={a.volatility_annual == null ? "—" : pct(a.volatility_annual, 1)} h="annualised, daily NAV" />
          <Metric sm k="Max drawdown" v={a.max_drawdown == null ? "—" : pct(a.max_drawdown, 1)} />
        </div>
        {a.rolling_1y && <p className="ink2" style={{ fontSize: 14, marginTop: 10 }}>Rolling 1-year returns ({a.rolling_1y.windows} windows): min {pct(a.rolling_1y.min, 1)}, median {pct(a.rolling_1y.median, 1)}, max {pct(a.rolling_1y.max, 1)}; {pct(a.rolling_1y.share_negative, 0)} of windows lost money.</p>}
        {x.series.dates.length > 1 && <LineChart x={x.series.dates} series={[{ name: "NAV", values: x.series.nav }]} height={240} />}
        <KV items={[["Scheme code", x.scheme.code], ["ISIN (growth / payout)", x.scheme.isin_growth || "—"], ["Plan / option", `${x.scheme.plan} / ${x.scheme.option}`]]} />
      </Panel>
      <Panel title="What a SIP in this fund actually returned" id="hist-sip" prov={res && <Prov source="AMFI NAVs + planning engine" period={[res.allotments?.[0]?.date, res.valuation_date]} note={res.rules} />}>
        <div className="row" style={{ marginBottom: 10 }}>
          <label>Monthly ₹<input type="number" value={sip.monthly} onChange={(e) => setSip({ ...sip, monthly: Number(e.target.value) })} /></label>
          <label>From<input type="date" value={sip.start} onChange={(e) => setSip({ ...sip, start: e.target.value })} /></label>
          <label>To<input type="date" value={sip.end} onChange={(e) => setSip({ ...sip, end: e.target.value })} /></label>
          <label>Yearly step-up %<input type="number" value={sip.step_up} onChange={(e) => setSip({ ...sip, step_up: Number(e.target.value) })} /></label>
          <button className="primary" onClick={run}>Simulate</button>
        </div>
        <Err e={err} />
        {res && <div className="metrics">
          <Metric k="Value" v={inr(res.value)} h={`on ${res.valuation_date} at NAV ${num(res.valuation_nav, 4)}`} />
          <Metric sm k="Invested" v={inr(res.invested)} h={`${res.instalments} instalments`} />
          <Metric sm k="XIRR" v={res.xirr == null ? "—" : pct(res.xirr, 2)} tone={res.xirr == null ? undefined : res.xirr >= 0 ? "up" : "down"} />
          <Metric sm k="Units" v={num(res.units, 3)} h={`stamp duty ${inr(res.stamp_duty, 2)}`} />
        </div>}
      </Panel>
    </div>
  );
}

export default function Funds() {
  const st = useApi<any>("/funds/status", 60000);
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const list = useApi<any>(`/funds/schemes?q=${encodeURIComponent(q)}&category=${encodeURIComponent(cat)}&limit=200`);
  const [sel, setSel] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const imp = async () => {
    setErr(null); setMsg(null);
    try { const r = await post("/funds/import", { text, file: "pasted" }); setMsg(`${r.layout} file: ${r.schemes} schemes, ${r.new_navs} new NAVs, ${r.duplicates} already stored, ${r.skipped} skipped.`); setText(""); st.reload(); list.reload(); }
    catch (e: any) { setErr(e.message); }
  };
  const s = st.data;
  const cols: Col[] = [{ key: "name", label: "scheme" }, { key: "category", label: "category" }, { key: "plan", label: "plan" },
    { key: "nav_points", label: "NAVs", r: true }, { key: "last_date", label: "last NAV" }];
  return (
    <>
      <div className="top"><h1>Mutual Funds</h1></div>
      <p className="sub">Fund analytics from daily NAVs published by AMFI: returns, volatility, drawdown, rolling returns, and what a SIP actually delivered. Mutual funds have one price a day; nothing here is intraday.</p>
      <Err e={st.error} />
      {s && s.navs === 0 && <Unavailable title="No NAV data imported yet">
        Download AMFI's daily NAV file (NAVAll.txt) or NAV history reports from amfiindia.com, then import them with <span className="mono">nseql import-amfi &lt;files&gt;</span> or paste one below.
        This environment could not reach AMFI, so the importer has been tested on the documented file layout only.</Unavailable>}
      {s && s.navs > 0 && <div className="metrics" style={{ marginBottom: 14 }}>
        <Metric k="Schemes" v={s.schemes.toLocaleString("en-IN")} /><Metric k="NAV points" v={s.navs.toLocaleString("en-IN")} h={`${s.first} → ${s.last}`} />
        <Metric k="Last import" v={s.imports[0]?.at?.slice(0, 10) || "—"} h={s.imports[0] ? `${s.imports[0].file} (${s.imports[0].layout})` : ""} />
      </div>}
      <div className="grid g21" style={{ marginTop: 14 }}>
        <Panel title="Schemes" hint={list.data ? `${list.data.total.toLocaleString("en-IN")} match` : ""} id="schemes" prov={<Prov source="AMFI files imported by the operator" />}>
          <div className="row" style={{ marginBottom: 10 }}>
            <label>Search<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="name or scheme code" /></label>
            <label>Category<select value={cat} onChange={(e) => setCat(e.target.value)}><option value="">All</option>{(list.data?.categories || []).map((c: string) => <option key={c}>{c}</option>)}</select></label>
          </div>
          <VirtualTable cols={cols} rows={list.data?.rows || []} height={380} onRow={(r) => setSel(r.code)} selected={(r) => r.code === sel} empty="No schemes imported." />
        </Panel>
        <Panel title="Import a NAV file" id="import">
          <p className="ink2" style={{ fontSize: 14 }}>Paste the text of an AMFI NAVAll.txt or NAV history report. Re-importing is safe: stored NAVs are not duplicated.</p>
          <textarea rows={7} value={text} onChange={(e) => setText(e.target.value)} aria-label="AMFI NAV file text" placeholder="Scheme Code;ISIN Div Payout/ ISIN Growth;..." />
          <div style={{ marginTop: 8 }}><button className="primary" disabled={!text.trim()} onClick={imp}>Import</button></div>
          <Err e={err} />{msg && <Banner tone="good">{msg}</Banner>}
        </Panel>
      </div>
      {sel && <div style={{ marginTop: 14 }}><Scheme code={sel} /></div>}
    </>
  );
}
