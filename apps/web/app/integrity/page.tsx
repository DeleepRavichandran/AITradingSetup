"use client";
/* Evidence Integrity: the adversarial layer (services/screening/integrity.py). It tries to disprove the bot's hypotheses.
   Shows detected biases, contradicting evidence, failed hypotheses, validation results, unresolved weaknesses, promotion
   eligibility and the predeclared suspension rules. Data: /integrity. AI agreement is never treated as evidence. */
import { useState } from "react";
import { Tag, signedInr, signedPct } from "@/components/vega";
import { post, useApi } from "@/lib/api";

const HSTATUS: Record<string, string> = { survived: "ok", falsified: "no", abstain: "wait", "not measurable yet": "" };
const BSTATUS: Record<string, string> = { guarded: "ok", detected: "no", exposed: "wait", "not measurable yet": "" };
const pv = (p: any) => (p == null ? "—" : p < 0.0001 ? "< 0.0001" : Number(p).toFixed(4));

function Decision({ onDone }: { onDone: () => void }) {
  const [subject, setSubject] = useState("challenger");
  const [decision, setDecision] = useState("reject");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const go = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    try { const r = await post("/integrity/decision", { subject, decision, reason }); setMsg(`Recorded in the ledger (${String(r.digest).slice(0, 12)}).`); setReason(""); onDone(); }
    catch (e: any) { setMsg(`Not recorded: ${e.message}`); }
  };
  return (
    <form onSubmit={go} className="b" style={{ display: "grid", gap: 10 }} data-testid="decision-form">
      <div className="row">
        <label>Subject<input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="challenger, rules 1.4, H-EDGE" /></label>
        <label>Decision<select value={decision} onChange={(e) => setDecision(e.target.value)}><option value="reject">Reject</option><option value="approve">Approve</option></select></label>
      </div>
      <label>Reason (at least 20 characters)<textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} style={{ fontFamily: "var(--sans)", fontSize: 13 }} /></label>
      <div className="row" style={{ alignItems: "center" }}><button className="primary" disabled={reason.trim().length < 20}>Record decision</button>
        <span className="dim" style={{ fontSize: 12 }}>Operator key only. Recording an approval changes no rule: a change still ships as reviewed code.</span></div>
      {msg && <p style={{ margin: 0 }} className={msg.startsWith("Not") ? "down" : "up"}>{msg}</p>}
    </form>
  );
}

export default function Integrity() {
  const q = useApi<any>("/integrity", 120000);
  const d = q.data;
  const hy: any[] = d?.hypotheses || [];
  const v = d?.validation;
  const cnt = (k: string) => hy.filter((h) => h.status === k).length;
  return (
    <>
      <div className="vhead"><div><h1>Evidence Integrity</h1><p>{d?.objective || "The adversarial layer tries to disprove every hypothesis before anything is trusted."}</p></div>
        {d && <Tag tone={d.verdict === "edge not falsified" ? "ok" : d.verdict === "abstain" ? "wait" : "no"} testid="ei-verdict">{d.verdict}</Tag>}</div>
      {q.error && <div className="banner crit">Integrity report unavailable: {q.error}</div>}
      {!d && !q.error && <p className="empty">Running the checks…</p>}
      {d && <>
        <div className="insight" style={{ marginBottom: 14 }}><span className="g" aria-hidden="true">✳</span><div>
          <div className="l">Verdict</div><p data-testid="ei-plain"><b>{d.verdict_plain}</b></p>
          <span className="dim" style={{ fontSize: 12 }}>A good decision can lose money and a bad one can make money: outcomes alone decide nothing here.</span></div></div>
        {d.suspension.active.length > 0 && <div className="banner crit" role="alert" data-testid="ei-suspension"><b>Suspension rule fired:</b> {d.suspension.active.map((t: any) => `${t.rule} ${t.text} (${t.detail})`).join("; ")}. {d.suspension.action}.</div>}
        <div className="vmetrics">
          <div className="vm"><div className="l">Evidence so far</div><div className="n">{v.sessions}<small> sessions</small></div><div className="f">{v.round_trips} closed round trips (all incumbent accounts)</div></div>
          <div className="vm"><div className="l">Hypotheses</div><div className="n">{hy.length}</div><div className="f"><span className="up">{cnt("survived")} survived</span><span className="down">{cnt("falsified")} falsified</span><span>{cnt("abstain")} abstain</span></div></div>
          <div className="vm"><div className="l">Biases</div><div className="n">{d.biases.filter((b: any) => b.status === "detected").length}<small> detected</small></div>
            <div className="f">{d.biases.filter((b: any) => b.status === "exposed").length} exposed · {d.biases.filter((b: any) => b.status === "guarded").length} guarded</div></div>
          <div className="vm"><div className="l">Evidence ledger</div><div className={`n ${d.ledger.ok ? "up" : "down"}`}>{d.ledger.ok ? "Intact" : "Broken"}</div>
            <div className="f">{d.ledger.entries} hash-chained entries; append-only</div></div>
        </div>

        <section className="vp" aria-labelledby="hy-h">
          <div className="h"><div><h2 id="hy-h">Hypotheses and their pre-registered tests</h2><div className="sub2">Only sessions after registration count; earlier ones are exploratory and decide nothing</div></div></div>
          <div className="tw" style={{ ["--mh" as any]: "none" }}><table className="wrap" data-testid="ei-hypotheses">
            <thead><tr><th>Hypothesis</th><th>Status</th><th>Confirmatory evidence</th><th className="r">p</th><th>Falsified if</th></tr></thead>
            <tbody>{hy.map((h) => <tr key={h.id}>
              <td style={{ minWidth: 220 }}><b>{h.id}</b><span className="st">{h.statement}</span></td>
              <td><Tag tone={HSTATUS[h.status] ?? ""}>{h.status}</Tag><span className="st">{h.reason}</span></td>
              <td>{h.confirmatory ? (h.confirmatory.sessions ?? h.confirmatory.round_trips ?? 0) : 0} of {h.need || "—"}
                {h.exploratory && <span className="st">exploratory: {h.exploratory.sessions ?? h.exploratory.round_trips ?? h.exploratory.late_n ?? "—"}{h.exploratory.mean != null ? `, mean ${signedPct(h.exploratory.mean, 3)}` : ""}</span>}</td>
              <td className="r">{pv(h.p)}</td>
              <td style={{ minWidth: 260, fontSize: 12.5 }} className="ink2">{h.falsification}<span className="st">registered {String(h.registered_at).replace("T", " ").slice(0, 16)}</span></td>
            </tr>)}</tbody>
          </table></div>
        </section>

        <div className="grid g2" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="ct-h">
            <div className="h"><div><h2 id="ct-h">Contradicting evidence</h2><div className="sub2">The same checks run against every claim</div></div></div>
            <div className="b"><ul className="gates" data-testid="ei-critic">{d.critic.length ? d.critic.map((c: any) => <li key={c.check}>
              <i style={{ background: c.survives ? "var(--good)" : "var(--crit)" }} /><span><b>{c.check}</b><span className="st">{c.detail}</span></span>
              <span className="r">{c.survives ? "claim survives" : "contradicts"}</span></li>) : <li>No closed trades yet to challenge.</li>}</ul></div>
          </section>
          <section className="vp" aria-labelledby="wk-h">
            <div className="h"><div><h2 id="wk-h">Unresolved weaknesses</h2><div className="sub2">{d.failed.length} failed hypothes{d.failed.length === 1 ? "is" : "es"} kept on record</div></div></div>
            <div className="b"><ul className="list" data-testid="ei-weak">{d.weaknesses.length ? d.weaknesses.map((w: string, i: number) => <li key={i} className="t" style={{ fontSize: 13 }}>{w}</li>) : <li className="dim">None recorded.</li>}</ul></div>
          </section>
        </div>

        <section className="vp" style={{ marginTop: 14 }} aria-labelledby="bi-h">
          <div className="h"><div><h2 id="bi-h">Bias audit</h2><div className="sub2">Each bias with the guard that addresses it and a deterministic check on the real ledger</div></div></div>
          <div className="tw" style={{ ["--mh" as any]: "none" }}><table className="wrap" data-testid="ei-biases">
            <thead><tr><th>Bias</th><th>Status</th><th>Guard</th><th>What the check found</th></tr></thead>
            <tbody>{d.biases.map((b: any) => <tr key={b.bias}><td style={{ minWidth: 150 }}><b>{b.bias}</b><span className="st">{b.risk}</span></td>
              <td><Tag tone={BSTATUS[b.status] ?? ""}>{b.status}</Tag></td><td style={{ minWidth: 220, fontSize: 12.5 }} className="ink2">{b.guard}</td>
              <td style={{ minWidth: 220, fontSize: 12.5 }}>{b.evidence}</td></tr>)}</tbody>
          </table></div>
        </section>

        <div className="grid g3" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="st-h"><div className="h"><h2 id="st-h">Cost and delay stress</h2></div>
            <div className="b vlist">
              <div><span>Actual (after costs)</span><b>{signedInr(v.stress.actual_inr)}</b></div>
              <div><span>Costs doubled</span><b className={v.stress.survives_costs ? "up" : "down"}>{signedInr(v.stress.double_costs_inr)}</b></div>
              <div><span>Entry and exit one minute later</span><b className={v.stress.survives_delay ? "up" : "down"}>{signedInr(v.stress.one_minute_delay_inr)}</b></div>
            </div></section>
          <section className="vp" aria-labelledby="pl-h"><div className="h"><h2 id="pl-h">Placebo: random entries</h2></div>
            <div className="b vlist">
              <div><span>Bot, mean per rupee</span><b>{signedPct(v.placebo.actual_mean_ret, 3)}</b></div>
              <div><span>Random entries, mean</span><b>{signedPct(v.placebo.placebo_mean_ret, 3)}</b></div>
              <div><span>p ({v.placebo.draws?.toLocaleString("en-IN") || "—"} seeded draws)</span><b>{pv(v.placebo.p)}</b></div>
            </div></section>
          <section className="vp" aria-labelledby="dr-h"><div className="h"><h2 id="dr-h">Drift and multiple testing</h2></div>
            <div className="b vlist">
              <div><span>Deterioration alarm (Page-Hinkley)</span><b className={v.drift.alarm ? "down" : ""}>{!v.drift.decided ? `needs ${d.settings.min_trades} trades` : v.drift.alarm ? `yes, at trade ${v.drift.at}` : "no"}</b></div>
              <div><span>Configurations traded</span><b>{v.configs.configs_traded}</b></div>
              <div><span>Survive correction (BH {Math.round(d.settings.fdr_configs * 100)}%)</span><b>{v.configs.survive} of {v.configs.tested}</b></div>
            </div></section>
        </div>

        {v.regimes.length > 0 && <section className="vp" style={{ marginTop: 14 }} aria-labelledby="rg-h">
          <div className="h"><div><h2 id="rg-h">Regime sensitivity</h2><div className="sub2">Market regime at entry, from the trade journal</div></div></div>
          <div className="tw"><table><thead><tr><th>Regime</th><th className="r">Round trips</th><th className="r">Net</th><th className="r">Mean per rupee</th><th className="r">Share won</th></tr></thead>
            <tbody>{v.regimes.map((r: any) => <tr key={r.regime}><td>{r.regime}</td><td className="r">{r.round_trips}</td><td className="r">{signedInr(r.net_inr)}</td>
              <td className="r">{signedPct(r.mean_ret, 3)}</td><td className="r">{Math.round(r.share_won * 100)}%</td></tr>)}</tbody></table></div>
        </section>}

        <div className="grid g2" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="pr-h">
            <div className="h"><div><h2 id="pr-h">Promotion eligibility</h2><div className="sub2">{d.promotion.candidate ? `Candidate: ${d.promotion.candidate}` : "No challenger running"}</div></div>
              <Tag tone={d.promotion.eligible_for_operator_review ? "ok" : "wait"} testid="ei-eligible">{d.promotion.eligible_for_operator_review ? "Ready for operator review" : "Not eligible"}</Tag></div>
            <div className="b"><ul className="gates" data-testid="ei-gates">{d.promotion.gates.map((g: any) => <li key={g.gate}><i style={{ background: g.pass ? "var(--good)" : "var(--warn)" }} />{g.gate}<span className="r">{g.pass ? "pass" : "not yet"}</span></li>)}</ul>
              <p className="dim" style={{ fontSize: 12, margin: "8px 0 0" }}>{d.promotion.note}</p></div>
          </section>
          <section className="vp" aria-labelledby="su-h">
            <div className="h"><div><h2 id="su-h">When it must stop</h2><div className="sub2">Predeclared suspension rules, fixed in code before the data</div></div></div>
            <div className="b"><ul className="gates" data-testid="ei-suspend">{Object.entries(d.suspension.rules).map(([k, t]: any) => {
              const hit = d.suspension.triggered.find((x: any) => x.rule === k);
              return <li key={k}><i style={{ background: hit ? "var(--crit)" : "var(--good)" }} /><span><b>{k}</b> {t}{hit && <span className="st">{hit.detail}</span>}</span><span className="r">{hit ? "fired" : "clear"}</span></li>;
            })}</ul><p className="dim" style={{ fontSize: 12, margin: "8px 0 0" }}>Action: {d.suspension.action}.</p></div>
          </section>
        </div>

        <div className="grid g2" style={{ marginTop: 14 }}>
          <section className="vp" aria-labelledby="op-h">
            <div className="h"><div><h2 id="op-h">Operator decision</h2><div className="sub2">Human approval is recorded before any promotion</div></div></div>
            <Decision onDone={() => q.reload()} />
          </section>
          <section className="vp" aria-labelledby="lg-h">
            <div className="h"><div><h2 id="lg-h">Evidence ledger</h2><div className="sub2">Latest entries · nothing is ever rewritten</div></div></div>
            <ul className="events" data-testid="ei-ledger" tabIndex={0} aria-label="Evidence ledger entries, newest first">{[...(d.ledger_entries || [])].reverse().slice(0, 14).map((e: any) => <li key={e.seq}>
              <span className="t">{String(e.at).slice(5, 10)}</span><span className="d" style={{ color: e.kind === "result" ? (e.body.status === "falsified" ? "var(--crit)" : e.body.status === "survived" ? "var(--good)" : "var(--warn)") : "var(--violet)" }} />
              <div><b><span className="k">{e.kind.toUpperCase()}</span>{e.subject}</b><p>{e.body.status || e.body.statement || e.body.decision || e.body.detail || ""}{e.body.reason ? ` · ${e.body.reason}` : ""}</p></div></li>)}</ul>
          </section>
        </div>
        <p className="dim" style={{ fontSize: 12, marginTop: 14 }}>Report {d.version} · generated {String(d.generated_at).replace("T", " ")} · seed {d.settings.seed} · α {d.settings.alpha} · deterministic for the same ledgers. Agreement between AI systems is not evidence and is never read here.</p>
      </>}
    </>
  );
}
