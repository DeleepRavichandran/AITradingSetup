"""Is the new brain actually better? (docs/VALIDATION.md, "brain comparison"). Research variants, not approved methodologies.

Every variant is a separate paper portfolio with the same capital, risk limits, data and execution model, fed the same
minute bars. Only the decision policy differs:

  A  incumbent              the live rules as they are
  B  uncertainty-aware      A, but capital only for configurations whose forward evidence qualifies (positive shrunk
                            expected edge after costs); unqualified ones explore at a quarter size so evidence can accrue
  B0 matched control        A's own ranking, limited to B's average number of new trades per day AND scaled to B's
                            average ticket size: if B beats A only because it trades less or smaller, B0 does as well
  C  market context         A plus only context features / concepts with holdout-supported evidence (none qualify yet:
                            then C is identical to A and is reported as "not run: nothing independently supported")
  D  combined               B plus C's supported additions
  E  references             cash, NIFTY 50, buy and hold of the watchlist, the challenger (a simple opening-range rule)

Ablation: B vs A (does uncertainty-awareness help?), B vs B0 (selection quality vs merely trading less or smaller), C vs A, D vs B.
Each comparison is paired by session with a block-bootstrap interval; "ahead", "behind" or "indistinguishable".
"""
from __future__ import annotations

from dataclasses import replace

from nseql.screening.execution import PortfolioRules

VERSION = "bc-1.1"
# bc-1.1: B was fund_qualified_only + explore_fraction, but the cash refusal is checked before exploration, so B never traded
#         and could never earn the evidence to qualify (found in the screenshot review: 0 round trips). B is now
#         explore_fraction only: full size when qualified, a quarter size while evidence accrues.


def rules(variant: str, capital: float, b_entries_per_day: int | None = None, b_size_ratio: float | None = None) -> PortfolioRules:
    base = PortfolioRules.live(capital)
    if variant == "A":
        return base
    if variant == "B":
        return replace(base, explore_fraction=0.25)
    if variant == "B0":
        r = min(1.0, max(0.05, float(b_size_ratio))) if b_size_ratio else 1.0
        return replace(base, max_entries_day=max(1, int(b_entries_per_day or 1)), per_trade_pct=base.per_trade_pct * r)
    raise KeyError(variant)


def supported_additions(concepts: dict | None, features: dict | None) -> list[str]:
    """Context additions that may enter C/D: holdout-supported (or better) only."""
    ok = ("holdout-supported finding", "forward-supported finding", "promotion-eligible methodology")
    out = [f"concept:{r['concept']}" for r in (concepts or {}).get("results", []) if r.get("verdict") in ok]
    out += [f"feature:{r['feature']}" for r in (features or {}).get("results", []) if r.get("verdict") in ok]
    return sorted(set(out))


def mean_ticket(sch, sessions: list[str]) -> float | None:
    """Average filled buy value per order (rupees) on these sessions."""
    if not sessions:
        return None
    v = sch.store.db.execute("SELECT AVG(t) FROM (SELECT SUM(qty * price) t FROM paper_fills WHERE side='BUY' AND substr(ts,1,10) IN (%s) "
                             "GROUP BY order_key)" % ",".join("?" * len(sessions)), sessions).fetchone()[0]
    return float(v) if v else None


def size_ratio(sch_b, sch_a, sessions: list[str]) -> float | None:
    """B's average ticket as a fraction of A's: B0 is scaled by it so that it matches B's exposure, not only its count."""
    b, a = mean_ticket(sch_b, sessions), mean_ticket(sch_a, sessions)
    return (b / a) if a and b else None


def entries_per_day(sch, sessions: list[str]) -> float:
    db = sch.store.db
    n = db.execute("SELECT COUNT(*) FROM paper_orders WHERE side='BUY' AND status IN ('filled','partial') AND substr(fill_ts,1,10) IN (%s)"
                   % ",".join("?" * len(sessions)), sessions).fetchone()[0] if sessions else 0
    return n / max(len(sessions), 1)


def trade_quality(account) -> dict:
    """Opportunity-selection quality from the variant's own round trips: count, mean net, share won, losing trades."""
    from nseql.screening.integrity import round_trips
    rt = round_trips(account)
    if rt.empty:
        return {"round_trips": 0}
    return {"round_trips": int(len(rt)), "mean_net_inr": round(float(rt.net.mean()), 2), "mean_ret": round(float(rt.ret.mean()), 6),
            "share_won": round(float((rt.net > 0).mean()), 3), "losing_trades": int((rt.net < 0).sum()),
            "charges_inr": round(float(rt.charges.sum()), 2), "net_inr": round(float(rt.net.sum()), 2)}
