"use client";
/* Vega copilot drawer, available on every page (Ctrl/Cmd+K). Read-only: it answers from the lab's own
   records through /api/copilot/ask and links to the evidence. It cannot change anything; state-changing
   commands (kill switch, replay, research) live on the Vega page behind confirmation. */
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

type Turn = { q: string; a?: any; err?: string };

export default function Copilot({ onClose, page }: { onClose: () => void; page: string }) {
  const [q, setQ] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sugg, setSugg] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    input.current?.focus();
    api("/copilot/suggestions").then((d) => setSugg(d.questions)).catch(() => setSugg([]));
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  useEffect(() => { body.current?.scrollTo({ top: body.current.scrollHeight }); }, [turns]);
  const ask = async (text: string) => {
    const t = text.trim();
    if (!t || busy) return;
    setQ("");
    setBusy(true);
    setTurns((x) => [...x, { q: t }]);
    try {
      const a = await api(`/copilot/ask?q=${encodeURIComponent(t)}&page=${encodeURIComponent(page)}`);
      setTurns((x) => x.map((y, i) => (i === x.length - 1 ? { ...y, a } : y)));
    } catch (e: any) {
      setTurns((x) => x.map((y, i) => (i === x.length - 1 ? { ...y, err: e.message } : y)));
    } finally { setBusy(false); }
  };
  return (
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="vega-h" data-testid="copilot">
        <header>
          <h2 id="vega-h" style={{ margin: 0 }}>Vega</h2>
          <span className="dim" style={{ fontSize: 13 }}>global assistant · answers from platform-wide records (for one backtest run, use Analyst Chat)</span>
          <span style={{ flex: 1 }} />
          <button className="ghost" onClick={onClose} aria-label="Close Vega">Close</button>
        </header>
        <div className="body" ref={body} aria-live="polite">
          {!turns.length && <>
            <p className="ink2" style={{ margin: 0 }}>Ask about strategies, research, the paper portfolio or the data feed. Every answer cites what it read and what it did not.</p>
            <div className="sugg">{sugg.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}</div>
            <p className="dim" style={{ fontSize: 13, margin: 0 }}>Voice and actions (with confirmation) are on the <Link href="/deck" onClick={onClose}>Vega Deck</Link>.</p>
          </>}
          {turns.map((t, i) => (
            <div className="qa" key={i}>
              <div className="q">{t.q}</div>
              {t.err && <div className="a down" role="alert">{t.err}</div>}
              {!t.a && !t.err && <div className="a dim">Looking it up…</div>}
              {t.a && <div className="a" data-testid="copilot-answer">
                <p style={{ margin: 0 }}>{t.a.answer}</p>
                {t.a.suggestions && <div className="sugg" style={{ marginTop: 8 }}>{t.a.suggestions.map((s: string) => <button key={s} onClick={() => ask(s)}>{s}</button>)}</div>}
                {t.a.evidence?.length > 0 && <div className="ev" style={{ marginTop: 8 }}>{t.a.evidence.map((e: any, j: number) =>
                  <Link key={j} href={e.href} onClick={onClose} className="chip info"><i />{e.label}</Link>)}</div>}
                <div className="gr" style={{ marginTop: 8 }}>
                  {t.a.grounding?.read?.length ? `Read: ${t.a.grounding.read.join(", ")}. ` : ""}
                  {t.a.grounding?.did_not_read?.length ? `Did not read: ${t.a.grounding.did_not_read.join(", ")}. ` : ""}{t.a.method}.
                </div>
              </div>}
            </div>))}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); ask(q); }}>
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Vega…" aria-label="Ask Vega a question" maxLength={500} />
          <button className="primary" disabled={busy || !q.trim()}>Ask</button>
        </form>
      </div>
    </>
  );
}
