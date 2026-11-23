"use client";
/* Research trail: hypothesis -> code -> backtest -> walk-forward -> comparison -> trade replay -> experiment memory.
   Each step carries the selected strategy, dataset and run (as URL parameters) where the records support it. */
import Link from "next/link";
import { useEffect, useState } from "react";

const only = (o: Record<string, string | undefined>): Record<string, string> =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => !!v)) as Record<string, string>;
const STEPS: [string, string, (c: Ctx) => Record<string, string>][] = [
  ["/factory", "Hypothesis", (c) => only({ strategy: c.strategy || undefined })],
  ["/code", "Code", (c) => only({ strategy: c.strategy || undefined })],
  ["/backtest", "Backtest", (c) => only({ run: c.run || undefined, strategy: c.strategy || undefined, dataset: c.dataset || undefined })],
  ["/walkforward", "Walk-forward", (c) => only({ strategy: c.strategy || undefined, dataset: c.dataset || undefined })],
  ["/compare", "Comparison", (c) => only({ runs: c.run || undefined })],
  ["/replay", "Trade replay", (c) => only({ run: c.run || undefined })],
  ["/memory", "Experiment memory", (c) => only({ strategy: c.strategy || undefined })],
];

export type Ctx = { strategy?: string | null; dataset?: string | null; run?: string | null };

export function ResearchTrail({ here, ctx }: { here: string; ctx: Ctx }) {
  const c = { strategy: ctx.strategy || undefined, dataset: ctx.dataset || undefined, run: ctx.run || undefined };
  return (
    <nav aria-label="Research steps" className="trail" data-testid="research-trail">
      {STEPS.map(([href, label, q], i) => {
        const qs = new URLSearchParams(q(c)).toString();
        const on = href === here;
        return <span key={href} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {i > 0 && <span aria-hidden="true" className="dim">→</span>}
          <Link href={qs ? `${href}?${qs}` : href} aria-current={on ? "step" : undefined} className={on ? "on" : ""}>{label}</Link>
        </span>;
      })}
      {(c.strategy || c.dataset || c.run) && <span className="dim" style={{ marginLeft: 8, fontSize: 12.5 }}>
        carrying {[c.strategy && `strategy ${c.strategy}`, c.dataset && `dataset ${c.dataset}`, c.run && `run ${c.run.slice(0, 8)}`].filter(Boolean).join(" · ")}</span>}
    </nav>
  );
}

/** Read the carried context from the current URL (client only). */
export function readCtx(): Ctx {
  if (typeof window === "undefined") return {};
  const q = new URLSearchParams(window.location.search);
  return { strategy: q.get("strategy"), dataset: q.get("dataset"), run: q.get("run") };
}

/** The carried context, read after mount (server and first client render agree, so no hydration mismatch). */
export function useCtx(): Ctx {
  const [c, setC] = useState<Ctx>({});
  useEffect(() => { setC(readCtx()); }, []);
  return c;
}
