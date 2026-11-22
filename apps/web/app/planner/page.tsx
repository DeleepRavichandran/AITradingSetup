"use client";
/* SIP & Goal Planner: projections at a return YOU assume (/api/planning/*). These illustrate compounding;
   they are not forecasts. Historical SIPs on real NAVs are on the Mutual Funds page. */
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { LineChart } from "@/components/charts";
import { Banner, Err, Metric, Panel, Prov, Segmented } from "@/components/ui";
import { api, inr, num } from "@/lib/api";

type Mode = "goal" | "sip" | "lumpsum" | "swp" | "stp";
const FIELDS: Record<Mode, [string, string, number, string][]> = {   // key, label, default, unit
  goal: [["target_today", "Goal in today's money", 5_000_000, "₹"], ["years", "Years", 15, "yrs"], ["annual_return", "Assumed return", 11, "%/yr"],
    ["inflation", "Inflation", 6, "%/yr"], ["existing", "Already invested", 0, "₹"], ["step_up", "Yearly SIP step-up", 0, "%"]],
  sip: [["monthly", "Monthly SIP", 10_000, "₹"], ["years", "Years", 10, "yrs"], ["annual_return", "Assumed return", 11, "%/yr"], ["step_up", "Yearly step-up", 0, "%"]],
  lumpsum: [["amount", "Amount", 500_000, "₹"], ["years", "Years", 10, "yrs"], ["annual_return", "Assumed return", 11, "%/yr"]],
  swp: [["corpus", "Starting corpus", 10_000_000, "₹"], ["monthly", "Monthly withdrawal", 60_000, "₹"], ["annual_return", "Assumed return", 8, "%/yr"],
    ["step_up", "Yearly withdrawal increase", 5, "%"], ["years", "Horizon", 40, "yrs"]],
  stp: [["corpus", "Amount in source fund", 1_200_000, "₹"], ["monthly", "Monthly transfer", 100_000, "₹"], ["source_return", "Source fund return", 6.5, "%/yr"],
    ["target_return", "Target fund return", 11, "%/yr"]],
};
const PCT = new Set(["annual_return", "inflation", "step_up", "source_return", "target_return"]);

export default function Planner() {
  const [mode, setMode] = useState<Mode>("goal");
  const [all, setAll] = useState<Record<Mode, Record<string, number>>>(() =>
    Object.fromEntries(Object.entries(FIELDS).map(([m, fs]) => [m, Object.fromEntries(fs.map(([k, , d]) => [k, d]))])) as Record<Mode, Record<string, number>>);
  const vals = all[mode];
  const setVals = (v: Record<string, number>) => setAll({ ...all, [mode]: v });
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const q = useMemo(() => new URLSearchParams(Object.fromEntries(FIELDS[mode].map(([k]) => [k, String(PCT.has(k) ? vals[k] / 100 : vals[k])]))).toString(), [mode, vals]);
  useEffect(() => {
    const id = setTimeout(async () => {
      try { setRes(await api(`/planning/${mode}?${q}`)); setErr(null); } catch (e: any) { setErr(e.message); setRes(null); }
    }, 250);
    return () => clearTimeout(id);
  }, [mode, q]);
  const sched = res?.schedule || [];
  return (
    <>
      <div className="top"><h1>SIP &amp; Goal Planner</h1></div>
      <p className="sub">Plan SIPs, step-up SIPs, lump sums, withdrawals (SWP) and transfers (STP) at a return you choose. For what a real fund actually delivered, use a fund's NAV history on <Link href="/funds">Mutual Funds</Link>.</p>
      <Banner tone="info"><b>Projections, not forecasts.</b> A constant assumed return never happens in practice; real returns vary year to year and can be negative. Taxes and exit loads are not included.</Banner>
      <div style={{ marginBottom: 14 }}>
        <Segmented label="Plan type" value={mode} onChange={(m) => setMode(m)} options={[["goal", "Goal"], ["sip", "SIP / step-up SIP"], ["lumpsum", "Lump sum"], ["swp", "SWP"], ["stp", "STP"]]} />
      </div>
      <div className="grid g12">
        <Panel title="Inputs" id="inputs">
          <form className="stack" onSubmit={(e) => e.preventDefault()}>
            {FIELDS[mode].map(([k, label, , unit]) => (
              <label key={k}><span>{label} <span className="dim">({unit})</span></span>
                <input type="number" inputMode="decimal" step="any" value={vals[k]} onChange={(e) => setVals({ ...vals, [k]: Number(e.target.value) })} />
              </label>))}
          </form>
        </Panel>
        <Panel title="Result" id="result" prov={res && <Prov source="planning engine (nseql.analytics.planning)" note={res.assumptions?.nature || "user-assumed returns; an illustration, not a forecast"} />}>
          <Err e={err} />
          {res && <div className="stack" data-testid="plan-result">
            {mode === "goal" && <div className="metrics">
              <Metric k="Monthly SIP needed" v={inr(res.monthly_sip_needed)} h={res.step_up ? `rising ${num(res.step_up * 100, 0)}% a year` : "constant"} />
              <Metric sm k="Goal in future money" v={inr(res.target_future)} h={`${res.years} years at ${num(res.assumptions.inflation * 100, 1)}% inflation`} />
              <Metric sm k="Existing money grows to" v={inr(res.existing_grows_to)} />
            </div>}
            {mode === "goal" && <p className="ink2" style={{ fontSize: 14, margin: 0 }}>The goal is inflated to future money, the growth of what you already hold is subtracted, and the
              remaining gap is divided by what ₹1 a month grows to at your assumed return{res.step_up ? ", with the instalment rising each year" : ""}. Change any input and the result updates.</p>}
            {(mode === "sip" || mode === "lumpsum") && <div className="metrics">
              <Metric k="Value at the end" v={inr(res.value)} />
              <Metric sm k="Invested" v={inr(res.invested)} h={`gain ${inr(res.gain)}`} />
              {res.xirr != null && <Metric sm k="XIRR" v={`${num(res.xirr * 100, 2)}%`} h="equals the assumed return by construction" />}
            </div>}
            {mode === "swp" && <div className="metrics">
              <Metric k="Corpus lasts" v={res.lasts} />
              <Metric sm k="Total withdrawn" v={inr(res.withdrawn)} />
              <Metric sm k="Left at the end" v={inr(res.balance)} />
            </div>}
            {mode === "stp" && <div className="metrics">
              <Metric k="Target fund value" v={inr(res.target_value)} h={`after ${res.months} transfers`} />
              <Metric sm k="Transferred" v={inr(res.transferred)} />
              <Metric sm k="Total" v={inr(res.total)} />
            </div>}
            {sched.length > 1 && (mode === "sip" ? <LineChart x={sched.map((r: any) => `year ${Math.ceil(r.month / 12)}`)} height={220}
              series={[{ name: "value ₹", values: sched.map((r: any) => r.value) }, { name: "invested ₹", values: sched.map((r: any) => r.invested) }]} />
              : mode === "swp" ? <LineChart x={sched.map((r: any) => `year ${Math.ceil(r.month / 12)}`)} height={220}
                series={[{ name: "balance ₹", values: sched.map((r: any) => r.balance) }, { name: "withdrawn ₹", values: sched.map((r: any) => r.withdrawn) }]} />
              : mode === "stp" ? <LineChart x={sched.map((r: any) => `month ${r.month}`)} height={220}
                series={[{ name: "target fund ₹", values: sched.map((r: any) => r.target) }, { name: "source fund ₹", values: sched.map((r: any) => r.source) }]} /> : null)}
          </div>}
        </Panel>
      </div>
    </>
  );
}
