"use client";
/* Brain Intelligence (services/screening/brain.py, observation only). What the brain knows, what it doubts, where it was
   wrong and corrected itself, and how each of its abilities measures against a simple baseline. Data: /brain.
   No combined intelligence score, no confidence percentages, no simulated agent activity. */
import { Tag, signedPct } from "@/components/vega";
import { useApi } from "@/lib/api";

const KTONE: Record<string, string> = { "observed fact": "info", "unverified hypothesis": "wait", "experimental finding": "ok", "validated finding": "ok",
  "contradicted finding": "no", "superseded finding": "", "invalidated finding": "no" };
const yn = (v: any) => (v === true ? "pass" : v === false ? "fail" : "unknown");

export default function Brain() {
  const q = useApi<any>("/brain", 120000);
  const d = q.data;
  const u = d?.uncertainty;
  return (
    <>
      <div className="vhead"><div><h1>Brain Intelligence</h1><p>What the brain knows, what it doubts, and where it corrected itself. Observation only: it changes no trade.</p></div>
        {d?.account && <Tag tone="vio">{d.version} · {d.account}</Tag>}</div>
      {q.error && <div className="banner crit">Brain report unavailable: {q.error}</div>}
      {!d && !q.error && <p className="empty">Reading the ledgers…</p>}
      {d && !d.account && <div className="banner info">{d.note}</div>}
      {d?.account && <>
        <div className="vmetrics">
          <div className="vm"><div className="l">Entries checked</div><div className="n">{u.entries}</div><div className="f">gates evaluated at each decision minute</div></div>
          <div className="vm"><div className="l">Would have abstained</div><div className="n">{u.would_abstain}<small> of {u.entries}</small></div><div className="f">failed at least one mandatory gate</div></div>
          <div className="vm"><div className="l">Knowledge entries</div><div className="n">{d.knowledge.length}</div><div className="f">{d.corrections.length} self-correction{d.corrections.length === 1 ? "" : "s"}, history kept</div></div>
          <div className="vm"><div className="l">Data quality</div><div className="n">{d.data_quality.quarantined}<small> quarantined</small></div><div className="f">{d.data_quality.conflicts} conflicting · {d.data_quality.late} late bars</div></div>
        </div>

        <div className="grid g2">
          <section className="vp" aria-labelledby="un-h">
            <div className="h"><div><h2 id="un-h">Uncertainty gates</h2><div className="sub2">{u.note}</div></div></div>
            <div className="b">
              {d.gates_now?.length > 0 && <><h3 style={{ marginTop: 0 }}>Right now</h3><ul className="gates" data-testid="gates-now">{d.gates_now.map((g: any) =>
                <li key={g.gate}><i style={{ background: g.ok ? "var(--good)" : g.ok === false ? "var(--crit)" : "var(--muted)" }} />{g.gate}<span className="r">{g.detail}</span></li>)}</ul></>}
              <h3>At past decisions</h3>
              <div className="tw"><table data-testid="gate-summary"><thead><tr><th>Gate</th><th className="r">Pass</th><th className="r">Fail</th><th className="r">Unknown</th></tr></thead>
                <tbody>{Object.entries(u.by_gate).map(([k, v]: any) => <tr key={k}><td>{{ fresh: "Data fresh", complete: "Data complete", evidence: "Positive expected edge after costs", familiar: "Familiar market conditions" }[k as string] || k}</td>
                  <td className="r">{v.pass}</td><td className="r">{v.fail}</td><td className="r">{v.unknown}</td></tr>)}</tbody></table></div>
              <p className="dim" style={{ fontSize: 12, margin: "8px 0 0" }}>{u.with_contradiction} entries had contradicting chart evidence at the time. "Unknown" is never counted as a pass.</p>
            </div>
          </section>
          <section className="vp" aria-labelledby="dq-h">
            <div className="h"><div><h2 id="dq-h">Decision quality versus outcome</h2><div className="sub2">{d.quality.note}</div></div></div>
            <div className="b vlist" data-testid="quality-matrix">{Object.entries(d.quality.matrix).map(([k, v]: any) => <div key={k}><span>{k}</span><b>{v}</b></div>)}
              {!Object.keys(d.quality.matrix).length && <div><span>No completed trade yet</span><b>—</b></div>}</div>
          </section>
        </div>

        <section className="vp" style={{ marginTop: 14 }} aria-labelledby="kn-h">
          <div className="h"><div><h2 id="kn-h">Knowledge</h2><div className="sub2">Versioned on the evidence ledger; a correction is a new version, never a deletion</div></div></div>
          <div className="tw" style={{ ["--mh" as any]: "420px" }}><table className="wrap" data-testid="knowledge">
            <thead><tr><th>Id</th><th>Status</th><th>Statement</th><th>Evidence</th><th>Limitations</th></tr></thead>
            <tbody>{d.knowledge.map((k: any) => <tr key={k.id}><td><b>{k.id}</b><span className="st">v{k.version} · {String(k.at).slice(0, 10)}</span></td>
              <td><Tag tone={KTONE[k.status] ?? ""}>{k.status}</Tag></td><td style={{ minWidth: 260 }}>{k.statement}
                {k.contradicting?.length > 0 && <span className="st">contradicted by: {k.contradicting.slice(0, 2).join("; ")}</span>}</td>
              <td style={{ fontSize: 12 }}>{Object.entries(k.evidence || {}).filter(([, v]) => v != null).map(([a, b]) => `${a} ${typeof b === "number" ? Math.round((b as number) * 1e4) / 1e4 : b}`).join(" · ") || "—"}</td>
              <td style={{ fontSize: 12, minWidth: 180 }} className="ink2">{k.limitations || "—"}</td></tr>)}</tbody>
          </table></div>
          {d.corrections.length > 0 && <div className="ft"><span>Corrections: {d.corrections.map((c: any) => `${c.id} ${c.from} → ${c.to}`).join("; ")}</span></div>}
        </section>

        <div className="grid g2" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="sh-h">
            <div className="h"><div><h2 id="sh-h">Counterfactual shadow outcomes</h2><div className="sub2">{d.shadow.kind}</div></div></div>
            {d.shadow.alternatives?.length ? <div className="tw"><table data-testid="shadow"><thead><tr><th>Alternative (fixed in advance)</th><th className="r">Mean per rupee</th><th className="r">Trades</th></tr></thead>
              <tbody>{d.shadow.alternatives.map((a: any) => <tr key={a.alternative}><td>{a.alternative}</td><td className="r"><span className={a.mean_ret >= 0 ? "up" : "down"}>{signedPct(a.mean_ret, 3)}</span></td><td className="r">{a.n}</td></tr>)}</tbody></table></div>
              : <p className="empty">No completed trade yet.</p>}
            <div className="ft"><span>{d.shadow.limitations}</span></div>
          </section>
          <section className="vp" aria-labelledby="dc-h">
            <div className="h"><div><h2 id="dc-h">Strategy decay and shared risk</h2><div className="sub2">Page-Hinkley per strategy type; correlated strategies count as one bet</div></div></div>
            <div className="tw" style={{ ["--mh" as any]: "260px" }}><table data-testid="decay"><thead><tr><th>Strategy type</th><th className="r">Trades</th><th className="r">1st half</th><th className="r">2nd half</th><th>Deterioration</th></tr></thead>
              <tbody>{d.decay.map((x: any) => <tr key={x.strategy_type}><td>{x.strategy_type}</td><td className="r">{x.round_trips}</td><td className="r">{signedPct(x.first_half_mean, 3)}</td>
                <td className="r">{signedPct(x.second_half_mean, 3)}</td><td>{x.deterioration === "alarm" ? <Tag tone="no">alarm</Tag> : x.deterioration}</td></tr>)}</tbody></table></div>
            <div className="ft"><span>At most {d.shared_risk.max_concurrent} positions open together · {d.shared_risk.same_stock_same_day_by_several_strategies} stock-days traded by several strategies · {d.shared_risk.highly_correlated_types?.length || 0} highly correlated pairs</span></div>
          </section>
        </div>

        <section className="vp" style={{ marginTop: 14 }} aria-labelledby="ms-h">
          <div className="h"><div><h2 id="ms-h">Measuring the brain itself</h2><div className="sub2">{d.principles}</div></div></div>
          <div className="tw" style={{ ["--mh" as any]: "none" }}><table className="wrap" data-testid="brain-metrics"><thead><tr><th>Measure</th><th>Now</th><th>Reference baseline</th><th>Evidence</th></tr></thead>
            <tbody>{d.metrics.map((m: any) => <tr key={m.measure}><td><b>{m.measure}</b></td><td>{m.value}</td><td className="ink2">{m.baseline}</td><td className="dim">{m.evidence}</td></tr>)}</tbody></table></div>
        </section>

        <section className="vp" style={{ marginTop: 14 }} aria-labelledby="ni-h">
          <div className="h"><div><h2 id="ni-h">Not built yet</h2><div className="sub2">Stated plainly so no page implies more than exists</div></div></div>
          <div className="b"><ul className="list" data-testid="not-implemented">{d.not_implemented.map((x: string) => <li key={x} className="t" style={{ fontSize: 13 }}>{x}</li>)}</ul></div>
        </section>
        <p className="dim" style={{ fontSize: 12, marginTop: 14 }}>Generated {String(d.generated_at).replace("T", " ")}. Shadow outcomes and gate splits use later prices and are research records only; no trading decision reads them.</p>
      </>}
    </>
  );
}
