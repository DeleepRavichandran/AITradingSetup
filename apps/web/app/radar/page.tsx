"use client";
/* Opportunity Radar: every watchlist stock's decision today, with the portfolio gates that decide whether any new buy is
   possible. Data: /radar (recorded decisions only; no later outcome is used). A row opens the decision dossier. */
import { useState } from "react";
import { Dossier, Filter, RadarTable, Tag, Usage } from "@/components/vega";
import { useApi } from "@/lib/api";

export default function Radar() {
  const rd = useApi<any>("/radar", 20000);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const r = rd.data, lim = r?.limits;
  const rows = (r?.rows || []).filter((x: any) => filter === "all" || x.state === filter);
  return (
    <>
      <div className="vhead"><div><h1>Opportunity Radar</h1><p>Every candidate needs a reason. Every refusal is recorded with its reason.</p></div>
        <Filter value={filter} onChange={setFilter} counts={r?.counts} /></div>
      {rd.error && <div className="banner crit">Radar unavailable: {rd.error}</div>}
      <div className="vwork">
        <section className="vp" aria-labelledby="rr-h">
          <div className="h"><div><h2 id="rr-h">Watchlist decisions {r?.day ? `· ${r.day}` : ""}</h2><div className="sub2">{r?.ranking || "Loading…"}</div></div></div>
          {r && <RadarTable rows={rows} onOpen={setOpen} testid="radar-full" />}
          <div className="ft"><span>{r?.note}</span><span>{r?.live_from ? `live decisions from ${String(r.live_from).replace("T", " ").slice(0, 16)}` : ""}</span></div>
        </section>
        <div className="vcol">
          <section className="vp" aria-labelledby="pg-h">
            <div className="h"><div><h2 id="pg-h">Portfolio gates</h2><div className="sub2">If any is closed, no new paper buy happens</div></div>
              <Tag tone={lim?.new_buys_possible ? "ok" : "wait"}>{lim ? (lim.new_buys_possible ? "Open" : "Closed") : "…"}</Tag></div>
            <div className="b"><ul className="gates" data-testid="portfolio-gates">
              {(lim?.blocked?.length ? lim.blocked : ["no portfolio-wide block"]).map((b: string) =>
                <li key={b}><i style={{ background: lim?.blocked?.length ? "var(--warn)" : "var(--good)" }} />{b}</li>)}
            </ul></div>
          </section>
          <section className="vp" aria-labelledby="ru-h">
            <div className="h"><div><h2 id="ru-h">Limits in use</h2><div className="sub2">Rules version {r?.limits?.rules ? "of this account" : ""}; changed only by the operator</div></div></div>
            <div className="b"><Usage usage={lim?.usage} /></div>
          </section>
          <section className="vp" aria-labelledby="cv-h">
            <div className="h"><div><h2 id="cv-h">Feed coverage</h2><div className="sub2">Point-in-time data for the decisions above</div></div></div>
            <div className="b vlist">
              <div><span>Last processed minute</span><b>{r?.coverage?.minute ? String(r.coverage.minute).slice(11, 16) + " IST" : "—"}</b></div>
              <div><span>Stocks with data</span><b>{r?.coverage?.symbols_with_data ?? "—"} / {r?.coverage?.symbols_expected ?? "—"}</b></div>
              <div><span>Catch-up minutes (not traded)</span><b>{r?.coverage?.catch_up_minutes ?? "—"} of {r?.coverage?.minutes ?? "—"}</b></div>
              <div><span>Decision time p95</span><b>{r?.coverage?.latency_p95_ms != null ? `${r.coverage.latency_p95_ms} ms` : "—"}</b></div>
            </div>
          </section>
        </div>
      </div>
      <p className="dim" style={{ fontSize: 12, marginTop: 14 }}>Intraday equities only today. Swing, positional, ETFs, funds and F&amp;O need their own data and validation before they appear here (docs/VISION.md).</p>
      <Dossier symbol={open} onClose={() => setOpen(null)} />
    </>
  );
}
