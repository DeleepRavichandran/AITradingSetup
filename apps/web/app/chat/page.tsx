"use client";
import { useEffect, useState } from "react";
import { Badge, Card, Err, Page } from "@/components/ui";
import { post, useApi, useQuery } from "@/lib/api";

const SUGGEST = ["Which hypotheses were rejected and why?", "Summarise the most recent run and its main caveats.", "What should the next experiment be?", "Which strategy family has been explored least?"];

export default function Chat() {
  const q = useQuery();
  const runs = useApi<any[]>("/runs?limit=40");
  const [run, setRun] = useState("");
  const [text, setText] = useState("");
  const [log, setLog] = useState<{ who: string; text: string; mode?: string; scope?: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (q.get("run")) setRun(q.get("run")!); }, [q]);
  const ask = async (question: string) => {
    if (!question.trim()) return;
    setBusy(true); setErr(null); setLog((l) => [...l, { who: "you", text: question }]); setText("");
    try { const r = await post("/chat", { question, run_id: run || null }); setLog((l) => [...l, { who: "analyst", text: r.answer, mode: r.mode, scope: run || "" }]); } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  return (
    <Page title="Analyst Chat" sub="Questions are answered only from the experiment registry. With an Anthropic API key configured on the server the answer is written by Claude; without one you get a plain digest of the same records, labelled as such. It never predicts profit."
      right={<select aria-label="Run in focus" value={run} onChange={(e) => setRun(e.target.value)}><option value="">no run in focus</option>{(runs.data || []).map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.strategy}</option>)}</select>}>
      <Err e={err} />
      <p className="ink2" style={{ margin: "0 0 10px" }} data-testid="chat-scope">Analyst Chat answers about <b>{run ? `run ${run}` : "the experiment registry as a whole"}</b>.
        For platform-wide questions (the live bot, positions, the market) use <b>Ask Vega</b> in the status bar.</p>
      <Card>
        {log.length === 0 && <div className="row">{SUGGEST.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}</div>}
        {log.map((m, i) => <div key={i} className={`msg ${m.who === "you" ? "me" : ""}`}><div className="dim" style={{ fontSize: 11, marginBottom: 4 }}>{m.who} {m.mode && <Badge s={m.mode} tone={m.mode.startsWith("llm") ? "good" : "warn"} />}
            {m.who === "analyst" && <span data-testid="answer-scope"> · about {m.scope ? <a href={`/backtest?run=${m.scope}`} className="mono">run {m.scope}</a> : "the whole experiment registry"}</span>}</div>
          <div style={{ whiteSpace: "pre-wrap", fontFamily: m.who === "analyst" && m.mode?.startsWith("digest") ? "var(--mono)" : "inherit", fontSize: m.mode?.startsWith("digest") ? 12 : 14 }}>{m.text}</div></div>)}
        <form className="row" onSubmit={(e) => { e.preventDefault(); ask(text); }} style={{ marginTop: 10 }}>
          <input style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask about runs, hypotheses, lessons…" />
          <button className="primary" disabled={busy || !text.trim()}>{busy ? "Thinking…" : "Ask"}</button>
        </form>
      </Card>
    </Page>
  );
}
