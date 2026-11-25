"use client";
/* Mission Control (VEGA overview). One bot, selective decisions, evidence before execution.
   Every figure is from the API: /platform/status, /today, /radar, /decisions, /benchmarks, /integrity, /market/pulse.
   Paper (simulated) trading only; real-money orders are disabled and cannot be enabled from here. */
import Link from "next/link";
import { useMemo, useState } from "react";
import { LineChart } from "@/components/charts";
import { sessionState } from "@/components/session";
import { Dossier, Filter, RadarTable, Stream, Tag, Usage, signedInr, signedPct } from "@/components/vega";
import { inr, pct, useApi } from "@/lib/api";

function Curve({ today, bench }: { today: any; bench: any }) {
  const [period, setPeriod] = useState<"session" | "all">("session");
  const curve = today?.charts?.equity_curve || [];
  const b = bench?.series;
  const all = useMemo(() => {
    if (!b || !bench?.sessions?.length) return null;
    const keys = ["account (after costs)", "cash", "NIFTY 50", "buy and hold watchlist"].filter((k) => b[k]);
    const cum = (d: number[]) => { let v = 1; return d.map((r) => (v *= 1 + r) - 1); };
    return { x: bench.sessions, series: keys.map((k) => ({ name: k === "account (after costs)" ? "bot (after costs)" : k, values: cum(b[k].daily) })) };
  }, [b, bench]);
  const first = curve[0]?.v, last = curve[curve.length - 1]?.v;
  const base = today?.charts?.day_open_value || first;
  return (
    <section className="vp" aria-labelledby="perf-h" data-testid="perf">
      <div className="h"><div><h2 id="perf-h">Account performance</h2><div className="sub2">Net of simulated costs · next to cash and the market</div></div>
        <div className="vseg" role="group" aria-label="Period">
          <button aria-pressed={period === "session"} onClick={() => setPeriod("session")}>Session</button>
          <button aria-pressed={period === "all"} onClick={() => setPeriod("all")}>Since start</button>
        </div></div>
      <div className="b">
        {period === "session" ? (curve.length > 1 ? <>
          <div style={{ display: "flex", gap: 12, alignItems: "baseline", marginBottom: 4 }}>
            <span style={{ fontSize: 20, fontWeight: 650, letterSpacing: "-.02em" }}>{inr(last)}</span>
            <span className={last >= base ? "up" : "down"} style={{ fontSize: 12.5 }}>{signedPct(base ? last / base - 1 : null)} on the session</span></div>
          <LineChart x={curve.map((c: any) => c.t)} series={[{ name: "bot ₹", values: curve.map((c: any) => c.v) }, { name: "cash (session open)", values: curve.map(() => base) }]}
            height={210} fmt={(v) => inr(v)} />
        </> : <p className="empty">The session line appears after the first minutes of trading.</p>)
          : all ? <LineChart x={all.x} series={all.series} height={210} fmt={(v) => signedPct(v, 1)} zero />
          : <p className="empty">No benchmark series yet: it needs at least one closed session.</p>}
      </div>
      <div className="ft"><span>Source: paper ledger{period === "all" && bench?.notes?.length ? ` · ${bench.notes[0]}` : ""}</span>
        <span>{period === "session" ? (today?.charts?.session || "") : bench?.plain || ""}</span></div>
    </section>
  );
}

export default function MissionControl() {
  const ps = useApi<any>("/platform/status", 15000);
  const t = useApi<any>("/today", 15000);
  const rd = useApi<any>("/radar", 20000);
  const dc = useApi<any>("/decisions?limit=30", 20000);
  const bm = useApi<any>("/benchmarks", 300000);
  const ig = useApi<any>("/integrity", 300000);
  const pu = useApi<any>("/market/pulse?limit=1", 60000);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const d = t.data, a = d?.account, s = ps.data, r = rd.data;
  const st = sessionState(d, t.error);
  const pulse = pu.data?.pulses?.[0];
  const cov = r?.coverage;
  const lim = r?.limits;
  const unreal = (d?.holdings || []).reduce((x: number, h: any) => x + (h.unrealised_inr || 0), 0);
  const loss = lim?.usage?.find((u: any) => u.name === "Daily loss");
  const trades = lim?.usage?.find((u: any) => u.name === "New trades today");
  const rows = (r?.rows || []).filter((x: any) => filter === "all" || x.state === filter);
  const blocked: string[] = lim?.blocked || [];
  const weakest = ig.data?.weaknesses?.[0];

  return (
    <>
      <div className="vhead">
        <div><h1>Mission Control</h1><p>One bot. Selective decisions. Evidence before execution.</p></div>
        <div className="row" style={{ alignItems: "center" }}>
          <Tag tone={st.tone === "good" ? "ok" : st.tone === "crit" ? "no" : st.tone === "warn" ? "wait" : "info"} testid="mc-state">{st.label}</Tag>
        </div>
      </div>

      <div className="mstrip" data-testid="market-strip">
        <div><div className="l">Session</div><strong className={s?.market?.open ? "up" : ""}>{s ? `Market ${s.market.state}` : "—"}</strong>
          <span className="s">{s?.market?.ist_time ? String(s.market.ist_time).slice(11, 16) + " IST" : ""}</span></div>
        <div><div className="l">Bot</div><strong>{d?.bot?.state === "ok" ? "Working" : d?.bot?.state || "—"}</strong><span className="s">{hhmmOf(s?.last_candle)}</span></div>
        <div><div className="l">Watchlist breadth</div>{pulse ? <strong><span className="up">{pulse.breadth.up} up</span><span className="down">{pulse.breadth.down} down</span></strong> : <strong className="dim">no pulse yet</strong>}
          <span className="s">{pulse ? `of ${pulse.breadth.symbols}` : ""}</span></div>
        <div><div className="l">Leaders</div><strong>{pulse?.top_gainers?.[0] ? `${pulse.top_gainers[0][0]} ${pulse.top_gainers[0][1] >= 0 ? "+" : ""}${pulse.top_gainers[0][1].toFixed(2)}%` : "—"}</strong>
          <span className="s">{pulse?.top_losers?.[0] ? `${pulse.top_losers[0][0]} ${pulse.top_losers[0][1].toFixed(2)}%` : ""}</span></div>
        <div><div className="l">Data coverage</div><strong className={cov?.symbols_with_data != null && cov.symbols_with_data < cov.symbols_expected ? "" : "up"} data-testid="coverage">
          {cov?.symbols_with_data != null ? `${cov.symbols_with_data}/${cov.symbols_expected} with data` : "—"}</strong>
          <span className="s">{cov?.minutes ? `${cov.minutes} min · ${cov.catch_up_minutes} catch-up` : ""}</span></div>
      </div>

      {blocked.length > 0 && <div className="banner" role="status" data-testid="blocked">New paper buys are blocked: <b>{blocked.join("; ")}</b>. Protective exits remain available.</div>}

      <div className="vmetrics">
        <div className="vm"><div className="l">Paper account value <span className="dim">INR</span></div><div className="n" data-testid="mc-value">{a ? inr(a.value_now) : "—"}</div>
          <div className="f"><span className={a && a.today >= 0 ? "up" : "down"}>{a ? `${signedInr(a.today)} today` : ""}</span>{a?.return_since_start != null && <span>{signedPct(a.return_since_start)} since inception</span>}</div></div>
        <div className="vm"><div className="l">Session P&amp;L</div><div className={`n ${a && a.today >= 0 ? "up" : "down"}`}>{a?.day_open_equity ? signedPct(a.today / a.day_open_equity) : "—"}</div>
          <div className="f">{a ? `${signedInr(a.today_realised)} realised · ${signedInr(unreal)} open · ${inr(a.today_charges)} charges` : ""}</div></div>
        <div className="vm"><div className="l">Capital deployed</div><div className="n">{a?.exposure != null ? <>{(a.exposure * 100).toFixed(1)}<small>%</small></> : "—"}</div>
          <div className="f">{a ? `${inr(a.cash_free)} free cash` : ""}</div></div>
        <div className="vm"><div className="l">Daily loss budget</div><div className="n">{loss ? <>{pct(loss.used, 1)}<small> / {pct(loss.limit, 0)}</small></> : "—"}</div>
          <div className="f">{trades ? `${trades.used} of ${trades.limit} new trades used` : a?.halted_today ? "limit reached: no new buys today" : "within limit"}</div></div>
      </div>

      <div className="vwork">
        <div className="vcol">
          <Curve today={d} bench={bm.data} />
          <section className="vp" aria-labelledby="radar-h">
            <div className="h"><div><h2 id="radar-h">Opportunity radar</h2><div className="sub2">Ranked by decision, then expected edge after costs recorded at the time; not by signal count</div></div>
              <Filter value={filter} onChange={setFilter} counts={r?.counts} /></div>
            {rd.error ? <p className="empty">Radar unavailable: {rd.error}</p> : !r ? <p className="empty">Loading…</p> : <RadarTable rows={rows} onOpen={setOpen} limit={8} />}
            <div className="ft"><span>Select a row for its decision dossier.</span><Link href="/radar">Full radar</Link></div>
          </section>
        </div>
        <div className="vcol">
          <section className="vp" aria-labelledby="stream-h">
            <div className="h"><div><h2 id="stream-h">Decision stream</h2><div className="sub2">What the bot did and why, as recorded</div></div></div>
            <div className={`statusline ${blocked.length ? "warn" : ""}`}><i /><b>{blocked.length ? `New buys blocked · ${blocked[0]}` : d?.bot?.message || "Waiting for the lab…"}</b></div>
            <Stream events={dc.data?.events || []} />
            <div className="ft"><span>{dc.data?.day || ""}</span><Link href="/screening">Every decision in the ledger</Link></div>
          </section>
          <section className="vp" aria-labelledby="risk-h">
            <div className="h"><div><h2 id="risk-h">Risk &amp; execution</h2><div className="sub2">The account's approved limits; agents cannot change them</div></div>
              <Tag tone={blocked.length ? "wait" : "ok"}>{blocked.length ? "Buys blocked" : "Clear"}</Tag></div>
            <div className="b"><Usage usage={lim?.usage} /></div>
            <div className="ft"><span>Real-money order routing <b className={s?.live_trading_enabled ? "down" : ""}>{s?.live_trading_enabled ? "FLAG ON" : "disabled"}</b></span><Link href="/risk">Risk detail</Link></div>
          </section>
          <div className="insight" data-testid="research-brief"><span className="g" aria-hidden="true">✳</span><div>
            <div className="l">Evidence integrity</div>
            <p><b>{ig.data ? ig.data.verdict_plain : ig.error ? "Integrity report unavailable." : "Checking the evidence…"}</b>{weakest ? ` Weakest point: ${weakest}.` : ""}</p>
            <Link href="/integrity">Open the evidence lab</Link></div></div>
        </div>
      </div>
      <div className="foot2"><span>Source: this lab's paper ledger and research records. {d?.data_source?.label ? `Prices: ${d.data_source.label}.` : ""}</span>
        <span>Simulated orders only · no real broker orders · not investment advice</span></div>
      <Dossier symbol={open} onClose={() => setOpen(null)} />
    </>
  );
}

const hhmmOf = (ts?: string | null) => (ts ? `last candle ${String(ts).slice(11, 16)}` : "");
