"use client";
import { useState } from "react";
import { Badge, Banner, Card, DataTable, Err, KV, Loading, Page, Stat } from "@/components/ui";
import { num, short, useApi } from "@/lib/api";

export default function Data() {
  const dss = useApi<any[]>("/datasets");
  const [pick, setPick] = useState<string | null>(null);
  const [kind, setKind] = useState("EQ");
  const id = pick || dss.data?.[0]?.dataset_id || null;
  const d = dss.data?.find((x) => x.dataset_id === id);
  const det = useApi<any>(id ? `/datasets/${id}` : null);
  const q = useApi<any>(id ? `/datasets/${id}/quality?kind=${kind}` : null);
  const ev = useApi<any>("/events");
  return (
    <Page title="Data Quality & Coverage" sub="Every dataset is immutable and content-hashed. Results link back to the exact hash shown here. Quality problems are reported, never silently repaired.">
      <Err e={dss.error} /><Loading on={dss.loading} />
      <Card title="Datasets"><DataTable onRow={(r) => setPick(r.dataset_id)} selected={(r) => r.dataset_id === id} maxHeight={220} cols={[
        { key: "dataset_id", label: "id" }, { key: "synthetic", label: "kind", f: (r) => <Badge s={r.synthetic ? "synthetic" : "real"} tone={r.synthetic ? "warn" : "good"} /> },
        { key: "source" }, { key: "licence" }, { key: "instruments", r: true }, { key: "intervals", f: (r) => r.intervals.join(", ") },
        { key: "content_hash", label: "content hash", f: (r) => <span className="mono">{short(r.content_hash, 16)}</span> }]} rows={dss.data} empty="No datasets. Run `nseql fixtures` or import data." /></Card>
      {d && det.data && <div className="grid g2" style={{ marginTop: 14 }}>
        <Card title="Provenance">
          <KV items={[["description", d.description], ["source", d.source], ["licence", d.licence], ["redistribution", d.redistribution_allowed ? "allowed" : "not allowed"],
            ["raw source files", d.raw_files], ["ingest corrections", d.audit_entries], ["instruments by kind", Object.entries(d.kinds).map(([k, v]) => `${k} ${v}`).join(" · ")],
            ["range", Object.entries(d.range).map(([k, v]: any) => `${k}: ${String(v?.[0]).slice(0, 10)} → ${String(v?.[1]).slice(0, 10)}`).join(" · ")],
            ["content hash", <span key="h" className="mono">{d.content_hash}</span>]]} />
          {det.data.manifest.audit.length > 0 && <><h3>Corrections applied on ingest</h3><pre>{JSON.stringify(det.data.manifest.audit.slice(0, 20), null, 1)}</pre></>}
        </Card>
        <Card title="Research partition (fixed on first use)">
          {det.data.partition ? <KV items={[["development", `${det.data.partition.dev_start} → ${det.data.partition.dev_end}`], ["validation", `${det.data.partition.val_start} → ${det.data.partition.val_end}`],
            ["sealed", `${det.data.partition.sealed_start} → ${det.data.partition.sealed_end}`]]} /> : <div className="empty">No bars.</div>}
          <p className="dim" style={{ marginTop: 8 }}>Hypotheses are generated and tuned on development data, checked once on validation, and finalists get a single evaluation on the sealed window.</p>
          <h3>Corporate actions</h3>
          <DataTable maxHeight={170} cols={[{ key: "ex_date", label: "ex-date" }, { key: "symbol" }, { key: "kind" }, { key: "ratio", r: true }, { key: "amount", r: true }]} rows={det.data.corporate_actions} empty="None recorded." />
        </Card>
      </div>}
      {q.data && <div style={{ marginTop: 14 }}><Card title="Quality report" right={<select aria-label="Instrument kind" value={kind} onChange={(e) => setKind(e.target.value)}>{["EQ", "INDEX", "FUT", "OPT", "ALL"].map((k) => <option key={k}>{k}</option>)}</select>}>
        {q.data.integrity_failures.length > 0 && <Banner tone="crit">Integrity check FAILED for {q.data.integrity_failures.length} file(s): stored bytes no longer match the manifest.</Banner>}
        <div className="stats" style={{ marginBottom: 10 }}>
          <Stat k="Symbols checked" v={`${q.data.symbols_checked}/${q.data.symbols_total}`} h={q.data.truncated ? "truncated" : ""} />
          <Stat k="Sessions" v={q.data.sessions} h={`${q.data.first_session} → ${q.data.last_session}`} />
          <Stat k="Errors" v={q.data.issue_counts.error} tone={q.data.issue_counts.error ? "down" : undefined} /><Stat k="Warnings" v={q.data.issue_counts.warning} /><Stat k="Info" v={q.data.issue_counts.info} />
          <Stat k="Integrity" v={q.data.integrity_failures.length ? "FAILED" : "verified"} h="hash of every file" />
        </div>
        <p className="dim">Calendar: {q.data.calendar_source}</p>
        <div className="grid g2">
          <div><h3>Issues</h3><DataTable wrap maxHeight={300} cols={[{ key: "severity", f: (r) => <Badge s={r.severity} tone={r.severity === "error" ? "crit" : r.severity === "warning" ? "warn" : "info"} /> }, { key: "symbol" }, { key: "check" }, { key: "count", r: true }, { key: "detail" }]} rows={q.data.issues} empty="No issues found." /></div>
          <div><h3>Coverage</h3><DataTable maxHeight={300} cols={[{ key: "symbol" }, { key: "kind" }, { key: "first", f: (r) => r.first.slice(0, 10) }, { key: "last", f: (r) => r.last.slice(0, 10) }, { key: "bars", r: true, f: (r) => num(r.bars, 0) },
            { key: "missing_sessions", label: "missing", r: true }, { key: "zero_volume_bars", label: "zero-vol", r: true }]} rows={q.data.coverage} /></div>
        </div>
      </Card></div>}
      <Loading on={q.loading} what="Checking quality" /><Err e={q.error} />
      <div style={{ marginTop: 14 }}><Card title="News & event store">
        <p>{ev.data?.count ?? 0} events stored. {ev.data?.note}. Queries are always “as of” a timestamp, so a headline cannot be read before it was published.</p>
      </Card></div>
    </Page>
  );
}
