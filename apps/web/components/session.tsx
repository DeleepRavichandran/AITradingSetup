"use client";
/* Session decisions, shared by Mission Control and My Trading. Every count comes from /api/today's `session`
   (services/screening/metrics.session_decisions: BUY signals of one IST session by RECORDED allocation reason) and
   opens the Strategy Monitor ledger with the same date, horizon and reason filters. */
import Link from "next/link";
import { num } from "@/lib/api";

export type Conn = "connecting" | "offline" | "ready";

/** One operator state, in priority order. `d` is /api/today; `error` its fetch error. */
export function sessionState(d: any, error: any): { key: string; label: string; tone: string; text: string } {
  if (!d && !error) return { key: "connecting", label: "Connecting", tone: "info", text: "Waiting for the lab to report the bot's state…" };
  if (!d && error) return { key: "offline", label: "Lab offline", tone: "crit", text: "The dashboard cannot reach the lab API. Check that the lab is running on this computer." };
  const b = d.bot?.state;
  if (!d.started || b === "off") return { key: "startup", label: d.started ? "Bot off" : "Starting up", tone: "info", text: d.bot?.message || "The bot has not started yet." };
  if (b === "error") return { key: "error", label: "Could not start", tone: "crit", text: d.bot?.message || "" };
  if (b === "stopped") return { key: "stopped", label: "Paused", tone: "crit", text: d.bot?.message || "Kill switch engaged." };
  if (b === "stale") return { key: "stale", label: "Stale feed", tone: "warn", text: d.bot?.message || "No fresh candles." };
  if (b === "waiting") return { key: "waiting", label: "Waiting for the market", tone: "info", text: d.bot?.message || "The bot starts checking when the market opens." };
  const s = d.session;
  if (s && s.last_processed_minute && s.fills === 0 && !d.market?.open) return { key: "zero-trade", label: "No trades this session", tone: "warn", text: zeroReason(s) };
  if (d.market?.open) return { key: "market-open", label: "Market open", tone: "good", text: d.bot?.message || "" };
  return { key: "after-close", label: "After close", tone: "info", text: d.bot?.message || "" };
}

function zeroReason(s: any): string {
  if (!s.eligible) return s.warm_up ? "Only warm-up history was evaluated; no paper-tradable BUY signal occurred after the live start."
    : "No rule produced a BUY signal in this session.";
  const top = Object.entries(s.rejected_by_category || {})[0] as [string, number] | undefined;
  if (s.accepted && !s.fills) return `${num(s.accepted, 0)} BUY signals were accepted but no order filled (entries time out after a few minutes without volume).`;
  return top ? `${num(s.eligible, 0)} eligible BUY signals; none became a fill. Most were refused by ${top[0]} (${num(top[1], 0)}).`
    : `${num(s.eligible, 0)} eligible BUY signals; none became a fill.`;
}

export function whatHappened(d: any): string {
  const s = d?.session;
  if (!s || !s.last_processed_minute) return "No candle has been processed in this session yet.";
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
  if (s.day !== today) return `No candle processed today yet. Last session (${s.day}): ` + whatHappened({ ...d, session: { ...s, day: today, _last: s.day } }).replace(/^Session \S+, /, "");
  const t = String(s.last_processed_minute).slice(11, 16);
  if (s.fills === 0) return `Session ${s.day}, last candle ${t} IST: no trades. ${zeroReason(s)}`;
  const top = Object.entries(s.rejected_by_category || {})[0] as [string, number] | undefined;
  return `Session ${s.day}, last candle ${t} IST: ${num(s.eligible, 0)} eligible BUY signals, ${num(s.accepted, 0)} accepted, ${num(s.orders, 0)} orders, `
    + `${num(s.fills, 0)} fills${top ? `; the most common refusal was ${top[0]} (${num(top[1], 0)})` : ""}.`;
}

const ledger = (day: string, q: Record<string, string>) => `/screening?${new URLSearchParams({ day, decision: "BUY", ...q }).toString()}`;

export function SessionPanel({ s, className, rowClass, kClass, vClass }: { s: any; className?: string; rowClass?: string; kClass?: string; vClass?: string }) {
  if (!s) return <p className="dim" style={{ margin: 0 }}>Unavailable: the API did not report session decisions.</p>;
  const R = ({ k, v, href, title }: { k: string; v: number; href: string; title?: string }) => (
    <div className={rowClass || "kvrow"} style={rowClass ? undefined : { display: "flex", justifyContent: "space-between", gap: 10, padding: "5px 0", borderTop: "1px solid var(--border2)" }}>
      <span className={kClass} title={title}>{k}</span>
      <Link className={vClass} href={href} style={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }} aria-label={`${k}: ${num(v, 0)} - open the ledger`}>{num(v, 0)} →</Link>
    </div>);
  const d = s.day;
  return (
    <div className={className} data-testid="session-panel">
      <R k="Eligible paper BUY signals" v={s.eligible} href={ledger(d, { warmup: "exclude" })} title="BUY signals at or after the live start" />
      <R k="↳ accepted (given an order)" v={s.accepted} href={ledger(d, { status: "accepted" })} />
      <R k="↳ deferred (higher-ranked signal first)" v={s.deferred} href={ledger(d, { status: "deferred" })} />
      {Object.entries(s.rejected_by_category || {}).map(([cat, n]) => (
        <R key={cat} k={`↳ refused: ${cat}`} v={n as number} href={ledger(d, { status: "rejected", reason: cat })} />))}
      <R k="Orders placed" v={s.orders} href={`/screening?${new URLSearchParams({ day: d }).toString()}#orders`} />
      <R k="Fills (buys + sells)" v={s.fills} href={`/screening?${new URLSearchParams({ day: d }).toString()}#orders`} />
      <R k="Warm-up history (not tradable)" v={s.warm_up} href={ledger(d, { warmup: "only" })} title="decided on historical bars before the live start" />
      <p className="dim" style={{ fontSize: 12.5, margin: "8px 0 0" }}>Session {d} (IST). Counts use recorded allocation reasons; each opens the matching ledger rows.</p>
    </div>
  );
}
