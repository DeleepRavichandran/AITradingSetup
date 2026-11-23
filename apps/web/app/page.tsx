"use client";
/* Command Center: the state of the lab, from /api/command-center. Every panel ends in an evidence footer
   (source, period, update time, data quality). Nothing here is estimated in the browser. */
import Link from "next/link";
import { useState } from "react";
import { LineChart } from "@/components/charts";
import { MetricTiles, runCols } from "@/components/common";
import { Badge, Banner, DataTable, Err, Funnel, Metric, Panel, Prov, Skeleton, Spark, StatusGrid, Unavailable } from "@/components/ui";
import { inr, num, post, useApi, useJob } from "@/lib/api";


export default function CommandCenter() {
  const cc = useApi<any>("/command-center", 15000);
  const sys = useApi<any>("/system", 30000);
  const runs = useApi<any[]>("/runs?limit=8", 30000);
  const jobs = useApi<any[]>("/jobs", 5000);
  const news = useApi<any>("/news/live?limit=12", 60000);
  const pulse = useApi<any>("/market/pulse?limit=1", 60000);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, running } = useJob(jobId);
  const active = jobs.data?.find((j) => j.kind === "research" && j.status === "running");
  const start = async () => {
    setErr(null);
    try { setJobId((await post("/research/run", { max_iterations: 3, max_sealed_evaluations: 1 })).id); } catch (e: any) { setErr(e.message); }
  };
  const d = cc.data, p = d?.platform, s = d?.strategies?.screening, r = d?.strategies?.research, ai = d?.ai, pf = d?.portfolio;
  const ds = p?.data_source;
  const ready = p?.readiness?.filter((m: any) => m.status === "ready").length;
  const last = runs.data?.[0];
  return (
    <>
      <div className="top"><h1>Command Center</h1><span className="sp" />
        {d && !d.platform.research_service.available
          ? <button disabled title={d.platform.research_service.detail}>Research needs Docker (Linux sandbox)</button>
          : <button className="primary" onClick={start} disabled={!!active || running}>{active || running ? "Research running…" : "Run research (3 hypotheses)"}</button>}</div>
      <p className="sub">What the lab is doing now, how far each number can be trusted, and what needs attention. Simulated orders only.</p>
      <Err e={cc.error || err} />
      {!d && !cc.error && <Skeleton lines={4} />}
      {sys.data && !sys.data.auth_required && <Banner>Insecure local mode: the API answers this machine only, without a token.</Banner>}
      {p?.risk.kill_switch.engaged && <Banner tone="crit"><b>Kill switch engaged:</b> {p.risk.kill_switch.reason}. <Link href="/risk">Risk Control</Link></Banner>}
      {d && <>
        <div className="hero">
          <Panel title="Lab state" id="state" prov={<Prov source="runtime state" updated={d.generated_at} data={ds} />}>
            <p className="lede" data-testid="lede">
              {s ? <>Screening <em>{s.registered.toLocaleString("en-IN")} configurations</em> of {s.rules} rules on <em>{ds?.label?.toLowerCase()}</em>. </>
                : <>No screening run yet. </>}
              {pf ? <>Paper portfolio {inr(pf.equity_inr)}, {pf.return_pct >= 0 ? "+" : ""}{num(pf.return_pct, 2)}% since start. </> : null}
              {ready != null && <>{ready} of {p.readiness.length} modules ready.</>}
            </p>
            <div className="metrics" style={{ marginTop: 16 }}>
              <Metric k="Market" v={p.market.open ? "Open" : "Closed"} h={`${p.market.state} · IST ${p.market.ist_time.slice(11)}`} sm />
              <Metric k="Evaluated last cycle" v={s ? s.evaluated_last_cycle.toLocaleString("en-IN") : "—"} h={s ? `of ${s.registered.toLocaleString("en-IN")} registered` : "no run"} sm />
              <Metric k="Cycle latency p99" v={s?.latency_ms ? `${num(s.latency_ms.p99, 1)} ms` : "—"} h={s?.throughput_configs_per_s ? `${num(s.throughput_configs_per_s, 0)} evaluations/s` : ""} sm />
              <Metric k="Paper result after costs" v={pf ? inr(pf.equity_inr - pf.starting_capital_inr) : "—"} tone={pf ? (pf.equity_inr >= pf.starting_capital_inr ? "up" : "down") : undefined}
                h={pf?.drawdown ? `max drawdown ${num(pf.drawdown.max_pct, 2)}%` : ""} sm />
            </div>
            {pf && pf.equity_curve.values.length > 1 && <div style={{ marginTop: 14 }}>
              <div className="dim" style={{ fontSize: 13 }}>Paper equity, {pf.equity_curve.values.length} samples across the run</div>
              <Spark values={pf.equity_curve.values} label="paper equity across the run" />
            </div>}
          </Panel>
          <Panel title="Platform status" id="platform" prov={<Prov source="API runtime, trading calendar, sandbox probe" updated={d.generated_at} />}>
            <StatusGrid items={[
              { name: "Market data", status: ds?.stale ? "degraded" : ds?.verified ? "ready" : ds?.live ? "unverified" : ds?.kind === "none" ? "idle" : "ready", detail: ds?.label },
              { name: "Market session", status: p.market.open ? "open" : p.market.state, detail: `holiday calendar ${p.market.holiday_calendar}` },
              { name: "Broker (Angel One)", status: p.broker.angel_one?.status || "not configured", detail: p.broker.angel_one?.detail },
              { name: "Screening worker", status: p.workers.screening_loop.startsWith("running") ? "running" : "idle", detail: p.workers.screening_loop },
              { name: "Research sandbox", status: p.research_service.available ? "ready" : "blocked", detail: p.research_service.detail },
              { name: "Paper trading", status: p.paper_trading.screening_paper_orders === "on" ? "ready" : "disabled", detail: `${p.paper_trading.sessions} paper session(s); screening orders ${p.paper_trading.screening_paper_orders}` },
              { name: "Risk", status: p.risk.kill_switch.engaged ? "engaged" : p.risk.daily_loss_halt ? "degraded" : "ready", detail: p.risk.daily_loss_halt ? "daily loss limit hit: entries halted" : "kill switch clear; limits active" },
              { name: "Live orders", status: "disabled", detail: p.broker.live_orders },
            ]} />
            {p.workers.jobs_failed_recently.length > 0 && <p className="down" style={{ fontSize: 13, marginTop: 10 }}>Failed jobs: {p.workers.jobs_failed_recently.map((j: any) => `${j.kind} (${j.error})`).join("; ")}</p>}
          </Panel>
        </div>

        {(active || jobId) && <div style={{ marginBottom: 14 }}><Panel title="Research run in progress">
          <p><Badge s={job?.status || "running"} /> {job?.error && <span className="down">{job.error}</span>}</p>
          <DataTable maxHeight={180} cols={[{ key: "sender" }, { key: "recipient" }, { key: "task" }, { key: "hypothesis_id", label: "hypothesis" }]} rows={[...((job || active)?.progress || [])].reverse()} empty="Waiting for the first agent message…" />
          {job?.status === "done" && <p style={{ marginTop: 8 }}>Finished: {job.result.results.length} hypotheses, {job.result.candidates.length} candidate(s). <Link href="/factory">Open the Strategy Factory</Link></p>}
        </Panel></div>}

        <div className="grid g2" style={{ marginBottom: 14 }}>
          <Panel title="Strategy intelligence" id="strategy" right={<Link href="/universe">Strategy Universe</Link>}
            prov={s ? <Prov source="screening registry and cycle log" period={s.provenance.period} data={s.provenance.data} /> : <Prov source="research registry" />}>
            {s ? <>
              <div className="metrics">
                <Metric k="Registered" v={s.registered.toLocaleString("en-IN")} h={`${s.rules} rules · ${s.families} families`} sm />
                <Metric k="Qualified" v={s.by_stage.qualified} h={`${s.by_stage.gated} proven negative`} sm />
                <Metric k="By horizon" v={`${s.by_horizon.intraday ?? 0} / ${s.by_horizon.swing ?? 0}`} h="intraday / swing" sm />
                <Metric k="Asset class" v="Cash equity" h="the minute loop trades NSE equities only" sm />
              </div>
              <p className="dim" style={{ fontSize: 13, margin: "10px 0" }}>{s.note}</p>
              <h3>From registered to qualified</h3>
              <Funnel steps={s.funnel} />
              <h3>Cycle latency, last {s.latency_ms?.series.length ?? 0} minutes</h3>
              {s.latency_ms ? <Spark values={s.latency_ms.series} label={`cycle latency, p50 ${num(s.latency_ms.p50, 1)} ms, p99 ${num(s.latency_ms.p99, 1)} ms`} /> : <span className="dim">no cycles</span>}
            </> : <Unavailable title="No screening run yet">Start a replay on the <Link href="/screening">Intraday &amp; Swing Lab</Link>, or set up Angel One for live 1-minute data.</Unavailable>}
            {r && <><h3>Research pipeline</h3>
              <p style={{ fontSize: 14, margin: 0 }}>{r.backtests} backtests recorded · hypotheses {Object.entries(r.hypotheses_by_status).map(([k, v]) => `${String(k).replaceAll("_", " ")} ${v}`).join(" · ") || "none yet"}</p></>}
          </Panel>

          <Panel title="AI intelligence" id="ai" right={<Link href="/learning">AI Research Brain</Link>} prov={<Prov source="research registry and Learning Brain memory" data={undefined} note="research runs use the datasets recorded with each run" />}>
            <div className="metrics">
              <Metric k="Learning verdict" v={ai.learning_verdict === "out-of-sample improvement shown" ? "Improving" : "Not shown"} h={ai.learning_verdict} sm />
              <Metric k="Active experiments" v={ai.active_experiments.length} sm />
              <Metric k="Memory episodes" v={ai.memory.episodes ?? "—"} sm />
              <Metric k="Policy champion" v={ai.champion || "—"} h={`${ai.policy_versions.length} version(s) recorded`} sm />
            </div>
            <h3>Discoveries (passed audit and sealed check)</h3>
            {ai.discoveries.length ? <ul className="list">{ai.discoveries.map((h: any) => <li key={h.id}><Link href={`/factory?h=${h.id}`} className="mono">{h.id}</Link> <span className="ink2">{h.family}</span> <Badge s={h.status} /></li>)}</ul>
              : <p className="dim" style={{ margin: 0 }}>None yet.</p>}
            <h3>Failed hypotheses</h3>
            {ai.failed_hypotheses.length ? <ul className="list">{ai.failed_hypotheses.map((h: any) => <li key={h.id}><span className="mono">{h.id}</span> <Badge s={h.status} /><div className="d">{h.reason}</div></li>)}</ul>
              : <p className="dim" style={{ margin: 0 }}>None recorded.</p>}
            <h3>Latest lessons</h3>
            {ai.lessons.length ? <ul className="list">{ai.lessons.slice(0, 4).map((l: any, i: number) => <li key={i}><Badge s={l.outcome} /> <span className="t">[{l.family}] {l.text}</span>{l.next_step && <div className="d">Next: {l.next_step}</div>}</li>)}</ul>
              : <p className="dim" style={{ margin: 0 }}>No lessons yet: run research.</p>}
          </Panel>
        </div>

        <div className="grid g21" style={{ marginBottom: 14 }}>
          <Panel title="Portfolio intelligence" id="portfolio" right={<Link href="/screening">Paper ledger</Link>}
            prov={pf ? <Prov source={pf.provenance.source} period={pf.provenance.period} data={pf.provenance.data} /> : undefined}>
            {pf ? <>
              <div className="metrics">
                <Metric k="Equity" v={inr(pf.equity_inr)} h={`from ${inr(pf.starting_capital_inr)}`} />
                <Metric k="Available" v={inr(pf.available_inr)} h={`${inr(pf.reserved_inr)} reserved`} sm />
                <Metric k="Deployed" v={inr(pf.deployed_inr)} h={`${num(pf.utilisation_pct, 1)}% of equity`} sm />
                <Metric k="Open positions" v={pf.open_positions} h={pf.rules ? `limit ${pf.rules.max_positions}` : ""} sm />
                <Metric k="Drawdown" v={pf.drawdown ? `${num(pf.drawdown.current_pct, 2)}%` : "—"} h={pf.drawdown ? `max ${num(pf.drawdown.max_pct, 2)}%` : ""} sm />
              </div>
              <h3>Equity, last {pf.equity_curve.values.length} samples</h3>
              <LineChart x={pf.equity_curve.values.map((_: any, i: number) => String(i))} series={[{ name: "equity ₹", values: pf.equity_curve.values }]} height={170} />
              <p className="dim" style={{ fontSize: 13, margin: "6px 0 0" }}>Open positions by rule family: {Object.entries(pf.open_by_family).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"} ·
                largest holdings: {pf.top_symbols.map((x: any) => `${x.symbol} ${num(x.pct_equity * 100, 1)}%`).join(", ") || "none"}{pf.halted_today ? " · daily loss limit reached" : ""}</p>
            </> : <Unavailable title="No paper portfolio yet">The shared paper portfolio starts with the first screening run.</Unavailable>}
          </Panel>
          <Panel title="Module readiness" id="ready" prov={<Prov source="/api/readiness logic, evaluated now" updated={d.generated_at} />}>
            <StatusGrid items={p.readiness.map((m: any) => ({ name: m.module, status: m.status, detail: m.detail }))} />
          </Panel>
        </div>

        <div className="grid g21" style={{ marginBottom: 14 }} id="news">
          <Panel title="News watch" hint="headlines tagged to your stocks and themes; not used for trading"
            prov={news.data && <Prov source="public RSS feeds, polled every 5 minutes" updated={news.data.status?.last_news_poll?.at}
              note={`${news.data.status?.headlines_stored ?? 0} stored since ${String(news.data.status?.since || "—").slice(0, 16).replace("T", " ")}${Object.keys(news.data.status?.errors || {}).length ? ` · unavailable: ${Object.keys(news.data.status.errors).join(", ")}` : ""}`} />}>
            {!news.data?.status?.running && <Unavailable title="News watcher off">It starts with live screening (or set NSEQL_NEWS_WATCH=true).</Unavailable>}
            {news.data?.items?.length ? <ul className="list">{news.data.items.map((h: any) => <li key={h.id}>
              <a href={h.link} target="_blank" rel="noreferrer" className="t" style={{ color: "var(--ink)" }}>{h.title}</a>
              <div className="d">{String(h.first_seen).slice(11, 16)} · {h.source}{h.symbols.length ? <> · <span className="data">{h.symbols.join(", ")}</span></> : null}{h.themes.length ? ` · ${h.themes.join(", ")}` : ""}</div></li>)}</ul>
              : news.data?.status?.running ? <p className="dim" style={{ margin: 0 }}>No headlines stored yet; the first poll runs within a minute of start.</p> : null}
          </Panel>
          <Panel title="Market pulse" hint="every 15 minutes while the market is open" prov={pulse.data?.pulses?.[0] && <Prov source="screening loop's own bars and paper ledger" updated={pulse.data.pulses[0].at} note={pulse.data.pulses[0].data} />}>
            {pulse.data?.pulses?.[0] ? (() => { const x = pulse.data.pulses[0]; return <div className="stack">
              <div className="metrics">
                <Metric sm k="Breadth" v={`${x.breadth.up} ↑ / ${x.breadth.down} ↓`} h={`${x.breadth.symbols} symbols since the open`} />
                <Metric sm k="Paper equity" v={inr(x.paper_equity_inr)} h={`${x.open_positions} open · realised ${inr(x.realised_today_inr)}`} />
              </div>
              <p className="ink2" style={{ fontSize: 14, margin: 0 }}>Gainers: {x.top_gainers.map((m: any) => `${m[0]} ${m[1] > 0 ? "+" : ""}${num(m[1], 2)}%`).join(", ") || "—"}<br />
                Losers: {x.top_losers.map((m: any) => `${m[0]} ${num(m[1], 2)}%`).join(", ") || "—"}<br />
                BUY signals by family: {Object.entries(x.buy_signals_by_family).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}<br />
                News since the open: {x.news_since_open.headlines} headlines{Object.keys(x.news_since_open.by_symbol).length ? ` · ${Object.entries(x.news_since_open.by_symbol).slice(0, 4).map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}</p>
            </div>; })() : <p className="dim" style={{ margin: 0 }}>No pulse yet: the first one is taken during market hours once live screening runs.</p>}
          </Panel>
        </div>

        <div className="grid g21" style={{ marginBottom: 14 }}>
          <Panel title="Latest backtests" right={<Link href="/compare">compare</Link>} prov={<Prov source="research registry" note="data per run: see the data column" />}>
            <DataTable cols={runCols().filter((c) => !["kind", "created_at"].includes(c.key))} rows={runs.data} maxHeight={300} empty="No backtests yet. Run one in the Backtesting Studio." />
          </Panel>
          <Panel title="Policy versions" hint="champion vs challengers" prov={<Prov source="Learning Brain version log" />}>
            {ai.policy_versions.length ? <ul className="list">{ai.policy_versions.map((v: any) => <li key={v.id}><span className="mono">{v.id}</span> <Badge s={v.status} tone={v.status === "champion" ? "good" : v.status === "rejected" || v.status === "rolled_back" ? "crit" : "info"} />
              <div className="d">{v.origin}{v.forward_epochs ? ` · ${v.forward_epochs} forward epoch(s)` : ""}{v.reason ? ` · ${v.reason}` : ""}</div></li>)}</ul>
              : <p className="dim" style={{ margin: 0 }}>No research-policy versions yet.</p>}
          </Panel>
        </div>

        {last && <div style={{ marginBottom: 14 }}><Panel title={<>Most recent backtest · <Link href={`/backtest?run=${last.run_id}`} className="mono">{last.run_id}</Link> · {last.strategy}</>}
          prov={<Prov source="backtest run record" period={[last.period_start, last.period_end]} data={last.synthetic ? { synthetic: true } : { kind: "replay" }} />}>
          <MetricTiles m={last.metrics} /></Panel></div>}

        {sys.data && <Panel title="Capability status" hint="reported by the backend" prov={<Prov source="backend capability list" note="see docs/AUDIT_V6.md for the full matrix" />}>
          <DataTable maxHeight={420} wrap cols={[{ key: "feature" }, { key: "status", f: (x) => <Badge s={x.status} /> }, { key: "note" }]} rows={sys.data.features} />
        </Panel>}
      </>}
    </>
  );
}
