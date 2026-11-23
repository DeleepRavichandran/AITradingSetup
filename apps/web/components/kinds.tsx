/* Which kind of account a page is about. The lab has several, and they are easy to confuse. */
import Link from "next/link";

const KINDS: [string, string, string, string][] = [
  ["screening", "/screening", "Screening account", "the live paper bot: one shared pot, 1-minute candles, simulated orders"],
  ["replay", "/screening", "Replay sessions", "the same loop run over stored 1-minute bars (history or synthetic), never live"],
  ["paper", "/paper", "Daily paper sessions", "library strategies on end-of-day bars, one session each, simulated orders"],
  ["portfolio", "/portfolio", "Portfolio simulation", "a backtest of several strategies on one account; no live data"],
  ["brokers", "/brokers", "Broker connectivity", "data and order adapters; real-money orders stay switched off"],
];

export function AccountKinds({ here }: { here: string }) {
  return (
    <div className="kinds" role="list" aria-label="Kinds of account in this lab" data-testid="account-kinds">
      {KINDS.map(([k, href, name, what]) => (
        <Link key={k} href={href} role="listitem" className={`kind ${k === here ? "on" : ""}`} aria-current={k === here ? "page" : undefined}>
          <b>{k === here ? "● " : ""}{name}</b><span>{what}</span></Link>))}
    </div>
  );
}
