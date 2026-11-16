"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { runCols } from "@/components/common";
import { Badge, Card, DataTable, Err, KV, Loading, Page } from "@/components/ui";
import { num, pct, useApi, useQuery } from "@/lib/api";

const pick = (arts: any[], t: string) => arts?.filter((a) => a.type === t).slice(-1)[0]?.payload;

export default function Factory() {
  const q = useQuery();
  const hs = useApi<any[]>("/research/hypotheses", 8000);
  const lib = useApi<any>("/strategies");
  const [id, setId] = useState<string | null>(null);
  useEffect(() => { if (q.get("h")) setId(q.get("h")); }, [q]);
  const real = (hs.data || []).filter((h) => h.origin !== "system");
  const hid = id || real[0]?.id || null;
  const d = useApi<any>(hid ? `/research/hypotheses/${hid}` : null);
  const h = d.data?.hypothesis, spec = h?.spec;
  const audit = pick(d.data?.artifacts, "AuditReport"), dec = pick(d.data?.artifacts, "Decision"), ev = pick(d.data?.artifacts, "Evidence"),
    risk = pick(d.data?.artifacts, "RiskReport"), ready = pick(d.data?.artifacts, "ReadinessReport");
  return (
    <Page title="Strategy Factory" sub="Every hypothesis the team has proposed, with its declarative spec, the code generated from it, the evidence gathered, the independent audit and the decision. Nothing is deleted — failures are part of the record.">
      <Err e={hs.error} /><Loading on={hs.loading} />
      <Card title={`Hypotheses (${real.length})`}>
        <DataTable onRow={(r) => setId(r.id)} selected={(r) => r.id === hid} maxHeight={260} cols={[{ key: "id", f: (r) => <span className="mono">{r.id}</span> }, { key: "family" },
          { key: "status", f: (r) => <Badge s={r.status} /> }, { key: "origin" }, { key: "parent_id", label: "parent", f: (r) => r.parent_id || "—" },
          { key: "statement", label: "hypothesis", f: (r) => r.statement }, { key: "status_reason", label: "reason", f: (r) => r.status_reason || "—" }]} rows={real} empty="No hypotheses yet. Start a run on the AI Research Agents page." />
      </Card>
      {h && spec && <div className="grid g2" style={{ marginTop: 14 }}>
        <Card title={<>Specification · <span className="mono">{h.id}</span></>} right={d.data.code && <Link href={`/code?code=${d.data.code.code_id}`}>view generated code</Link>}>
          <p><b>Hypothesis.</b> {spec.hypothesis}</p><p><b>Would be refuted if:</b> {spec.falsification}</p>
          <KV items={[["family", spec.family], ["universe / interval", `${spec.universe} · ${spec.interval}`],
            ["features", spec.features.map((f: any) => `${f.name} = ${f.fn}(${f.source}, ${f.n})`).join("; ")], ["entry", <code key="e">{spec.entry}</code>], ["exit", <code key="x">{spec.exit}</code>],
            ["parameters", JSON.stringify(spec.params)], ["search grid", JSON.stringify(spec.param_grid)], ["sizing", `${spec.sizing.max_positions} slots, ${pct(spec.sizing.invest, 0)} invested`],
            ["stop / target / max hold", `${spec.stop_loss_pct ? pct(spec.stop_loss_pct, 0) : "none"} / ${spec.take_profit_pct ? pct(spec.take_profit_pct, 0) : "none"} / ${spec.max_hold ?? "none"}`],
            ["regime assumptions", spec.regime_assumptions.join("; ") || "—"], ["expected failure modes", spec.expected_failure_modes.join("; ") || "—"], ["rationale", spec.rationale || "—"],
            ["lineage", <>{spec.parent_id ? <a onClick={() => setId(spec.parent_id)} className="pill">parent {spec.parent_id}</a> : "root"}{d.data.children.map((c: string) => <a key={c} onClick={() => setId(c)} className="pill"> · child {c}</a>)}</>]]} />
        </Card>
        <Card title="Decision">
          {dec ? <><p><Badge s={dec.decision} /> {dec.reasons?.join(", ")}</p><pre>{dec.explanation}</pre></> : <div className="empty">Not decided yet.</div>}
          {dec?.sealed && <><h3>Sealed out-of-sample (single use)</h3><KV items={[["window", dec.sealed.window?.join(" → ")], ["Sharpe", num(dec.sealed.sharpe)], ["return", pct(dec.sealed.total_return)],
            ["max drawdown", pct(dec.sealed.max_drawdown)], ["trades", dec.sealed.trades], ["Sharpe change vs validation", num(dec.sealed.deterioration_vs_validation == null ? null : -dec.sealed.deterioration_vs_validation)]]} /></>}
        </Card>
        <Card title="Independent audit">
          {audit ? <><p><Badge s={audit.verdict} /></p>
            <DataTable wrap maxHeight={260} cols={[{ key: "severity", f: (r) => <Badge s={r.severity} tone={r.severity === "fail" ? "crit" : r.severity === "warn" ? "warn" : "info"} /> }, { key: "code" }, { key: "message" }]} rows={audit.findings} empty="No findings." />
            <h3>Why this may still be unreliable</h3><ul style={{ margin: 0, paddingLeft: 18 }}>{audit.reliability_caveats.map((c: string) => <li key={c}>{c}</li>)}</ul></> : <div className="empty">No audit yet.</div>}
          {risk && <><h3>Risk review · <Badge s={risk.verdict} /></h3><DataTable wrap maxHeight={140} cols={[{ key: "code" }, { key: "message" }]} rows={risk.findings} empty="Within limits." /></>}
          {ready && <><h3>Execution readiness · <Badge s={ready.status} /></h3><DataTable wrap maxHeight={200} cols={[{ key: "severity", f: (r) => <Badge s={r.severity} tone={r.severity === "fail" ? "crit" : r.severity === "warn" ? "warn" : "info"} /> }, { key: "code" }, { key: "message" }]} rows={ready.checks} /></>}
        </Card>
        <Card title="Evidence">
          {ev ? <KV items={[["chosen parameters", JSON.stringify(ev.chosen_params)],
            ["look-ahead trap", ev.lookahead?.passed == null ? "not run" : `${ev.lookahead.passed ? "passed" : "FAILED"} (${ev.lookahead.orders_checked} orders compared)`],
            ["sandbox", ev.isolation ? `network namespace ${ev.isolation.network_namespace ? "on" : "off"}, env scrubbed, ${ev.isolation.cpu_seconds}s CPU, ${ev.isolation.memory_mb} MB` : "—"],
            ["development", `Sharpe ${num(ev.dev?.metrics?.sharpe)}, ${ev.dev?.metrics?.trades ?? 0} trades, max DD ${pct(ev.dev?.metrics?.max_drawdown)}`],
            ["grid tried", `${ev.dev_grid?.length ?? 0} points: ${ev.dev_grid?.map((g: any) => num(g.sharpe)).join(", ")}`],
            ["cost sensitivity", ev.cost_sensitivity?.rows?.map((r: any) => `×${r.multiplier}: ${pct(r.total_return)}`).join(" · ") || "—"],
            ["neighbouring parameters", ev.stability?.neighbours ? `${pct(ev.stability.share_positive, 0)} profitable, worst Sharpe ${num(ev.stability.worst_neighbour_sharpe)}` : "—"],
            ["stress", Object.entries(ev.stress || {}).map(([k, v]: any) => `${k.replaceAll("_", " ")}: ${pct(v.total_return)}`).join(" · ") || "—"],
            ["baselines (Sharpe)", ev.baselines?.strategy ? `strategy ${num(ev.baselines.strategy.sharpe)} · buy & hold ${num(ev.baselines.buy_and_hold?.sharpe)} · random entry ${num(ev.baselines.random_entry?.sharpe)}` : "—"],
            ["validation", ev.validation?.sessions ? `Sharpe ${num(ev.validation.sharpe)}, ${ev.validation.trades} trades, return ${pct(ev.validation.total_return)}` : "not reached"],
            ["bootstrap", ev.bootstrap?.p_value == null ? "—" : `p = ${num(ev.bootstrap.p_value)}, 90% CI ${num(ev.bootstrap.ci_05)} … ${num(ev.bootstrap.ci_95)}`],
            ["deflated Sharpe", ev.deflated_sharpe?.dsr == null ? "—" : `${num(ev.deflated_sharpe.dsr)} after ${ev.deflated_sharpe.n_trials} trials`],
            ["walk-forward", ev.walk_forward?.oos ? `OOS Sharpe ${num(ev.walk_forward.oos.sharpe)}, ${ev.walk_forward.oos.positive_folds}/${ev.walk_forward.oos.folds} folds positive` : "—"],
            ["regimes (return)", Object.entries(ev.regimes || {}).filter(([, v]: any) => v?.total_return != null).map(([k, v]: any) => `${k.replaceAll("_", " ")}: ${pct(v.total_return)}`).join(" · ") || "—"]]} /> : <div className="empty">No evidence yet.</div>}
        </Card>
      </div>}
      {d.data?.runs?.length > 0 && <div style={{ marginTop: 14 }}><Card title="Underlying runs (dataset hash, parameters and logs are one click away)"><DataTable cols={runCols().filter((c) => c.key !== "data")} rows={d.data.runs} maxHeight={260} /></Card></div>}
      <div style={{ marginTop: 14 }}><Card title="Built-in strategy library">
        <DataTable wrap maxHeight={380} cols={[{ key: "name", f: (r) => <Link href={`/code?lib=${r.name}`}>{r.name}</Link> }, { key: "family" }, { key: "requires" }, { key: "hypothesis" },
          { key: "default_params", label: "defaults", f: (r) => <span className="mono">{JSON.stringify(r.default_params)}</span> }]} rows={lib.data?.library} />
      </Card></div>
    </Page>
  );
}
