# Objective and architecture (operator's clarification, 2026-11-25)

**The goal is ONE intelligent, evidence-driven trading platform, not a strategy factory.** It seeks sustainable
risk-adjusted returns after costs, across intraday, swing, positional and long-term horizons, and across equities,
ETFs, mutual funds and SIP, and later futures and options.

Strategy count is not a measure of success. Strategy quality is. Real-money execution stays disabled until it is
separately authorised and validated.

## What the platform must know, and where that knowledge lives

| Question | Component | Status |
|---|---|---|
| What to trade | Universe + opportunity generators per horizon | intraday screener exists; swing/positional/long-term to build |
| When to trade | Setups with an explicit thesis, decided on completed data only | minute engine, chart analyst, price action exist |
| **When not to trade** | Gate: no proven edge after costs, event risk, poor liquidity, regime mismatch, limits reached, late data | partly: cost and evidence gates, 09:46 blackout, 10-trade limit, catch-up, kill switch |
| Why an opportunity qualifies | Decision record: the thesis, the evidence behind it, the context, the reasons for and against | partly: allocator explanation, journal, chart read |
| How much capital | Portfolio constructor: risk budget, correlated (related) bets, cash allowed, within approved limits | single-book allocator exists; cross-horizon books to build |
| When to exit | Exit manager: thesis invalidated, stop, target or trailing, time; per setup | stops and square-off only; to extend |
| Whether conditions changed | Regime and context monitor: trend/volatility regime, breadth, VIX, events, sentiment | fundamental agent (crowd emotion), journal regime, events |
| Why decisions succeeded or failed | Daily review: per-trade root cause, loss study, chart/price-action factors | exists |
| Whether an improvement works | Validation: walk-forward, untouched holdout, placebo, stress, benchmarks; then **human approval** | experiment harness, contest, placebo tests exist |

## The decision cycle (one engine, all horizons)

```
market data, fundamentals, news, events  ──►  CONTEXT (regime, breadth, VIX, sentiment, event calendar)
                                                    │
UNIVERSE (what is tradable, liquid, allowed) ──► SETUPS per horizon (few, interpretable, each with thesis + invalidation)
                                                    │
                                   EVIDENCE MODEL: expected edge after costs, with uncertainty, by regime
                                                    │
                                   GATE: when NOT to trade (no edge, event risk, illiquid, limits, late data)
                                                    │
                                   PORTFOLIO: size within risk limits; related bets share one budget; cash is a position
                                                    │
                                   EXECUTION (paper) ─► EXIT MANAGER ─► DECISION RECORD (taken, rejected, missed)
                                                    │
                                   REVIEW (daily) ─► RESEARCH PROPOSALS ─► VALIDATION ─► HUMAN APPROVAL ─► versioned promotion
```

**Built so far:**
- `services/screening/benchmarks.py`: the account against cash, NIFTY 50, buy-and-hold and the trend rule (`GET /api/benchmarks`).
- `services/screening/opportunities.py`: the gate review per refusal reason, plus missed moves (`GET /api/research/opportunities`).
- Both are steps of the daily review and appear on the Daily Review page.
- `services/screening/integrity.py`: the adversarial evidence-integrity layer (docs/INTEGRITY.md). It covers pre-registered hypotheses, a hash-chained ledger, bias audit, placebo, stress, drift, promotion gates and predeclared suspension. It appears on the dashboard as Research → Evidence Integrity.
- `services/screening/radar.py`: the opportunity radar, decision dossier and decision stream for the VEGA dashboard (`/api/radar`, `/api/radar/{symbol}`, `/api/decisions`). They show recorded decisions only.

## Rules of the cycle
1. **Every decision is recorded**, including rejected signals, each with its reason. Their later outcomes are
   attached **only in the research store**, as labelled hindsight. They never feed a live decision.
2. **No change after a single losing day.** A proposal needs evidence over a predeclared window, from data it was not
   designed on. It must beat the current rules, cash and the relevant index after costs, and survive stress (doubled
   slippage, a one-bar delay) and a placebo. Only then is it proposed to the operator, who approves or rejects it.
3. **No forced trades**, no daily profit targets, and no optimising win rate on its own. Holding cash is a valid decision.
4. **Benchmarks, always shown side by side:**
   - cash
   - the relevant index (NIFTY 50 for large caps; a sector index where relevant)
   - simple strategies: buy and hold the universe, and a plain trend-following rule
   - the current incumbent
5. **Headline metrics:**
   - net return after costs
   - risk-adjusted return (Sharpe, Sortino)
   - maximum drawdown
   - turnover and cost drag
   - capacity
   - stability across regimes
   - **Not** strategy count, signal count or win rate alone.
6. **Safety stays fixed:**
   - real money is off
   - the risk limits change only by the operator's decision (daily loss cap, position size, the 10-trades-a-day cap)
   - the kill switch is human-reset only

## The existing infrastructure in the new design
- **The 1-minute engine** (incl. 50k-scale screening) becomes the **intraday opportunity scanner and execution simulator**.
  Its registry of rule variants becomes a **research candidate pool**: variants compete for evidence, but the live book
  trades only setups that passed validation and approval.
- **Allocator, risk limits, kill switch, execution 2.0** (fills, charges, liquidity) are reused by every book.
- **Chart Analyst, price action, Fundamental Agent, events, news watch** are inputs to context, setups and the gate.
- **Daily review, loss study, RCA, experiment harness, forward contest, placebo tests** form the learning and validation loop.
- **Backtester** (daily bars, margin model), **planner and mutual-fund NAVs** feed the swing, positional, long-term and SIP books.
- **Learning Brain:** episodic memory of experiments; its out-of-sample benchmark is the bar for "learning helps".

## Roadmap
Each phase ships tested code and needs the operator's approval before any rule change trades.

| Phase | Deliverable | Done when |
|---|---|---|
| 0 ✅ | This objective. Strategy count removed as a headline (bot status shows the last candle, not strategies checked). Benchmark panel: cash, NIFTY 50, buy-and-hold watchlist, simple trend rule, incumbent. | dashboards and the daily review show results vs benchmarks after costs |
| 1 ✅ (first version) | **Decision record and opportunity ledger** (`services/screening/opportunities.py`, research store `experiments/research/opportunities.sqlite`, isolation test): every BUY/EXIT/REJECT with reason and context; rejected and missed opportunities tracked forward in the research store only | the review lists taken, rejected and missed with outcomes, and a test proves no hindsight leaks into decisions |
| 2 | **Setup library and gate:** a small set of interpretable setups per horizon (e.g. breakout-retest, trend pull-back, mean reversion in range, quality-momentum), each with thesis, invalidation and exit; a "when not to trade" gate on context and regime | each setup validated walk-forward on real data against benchmarks, then approved |
| 3 | **Swing and positional book on daily data:** the laptop already has two years of Angel One daily candles; fundamentals from results imports; first real-data walk-forward and holdout | results vs NIFTY, buy-and-hold and cash after costs, with intervals |
| 4 | **Portfolio across books:** one risk budget split between intraday, swing, positional and long-term; correlation-aware; cash as a position | portfolio-level drawdown and risk limits hold in replay and forward paper |
| 5 | **Long-term holdings, ETFs, mutual funds, SIP:** planner and NAV module joined to the portfolio | rebalancing and SIP decisions recorded and benchmarked |
| 6 | **Futures and options:** data (chains, margins), hedging first, then directional; strict risk caps | needs a licensed data source, and validation in paper first |

Real-money execution is a separate project. It needs explicit operator authorisation, broker validation, and the
live-readiness checklist (`docs/LIVE_READINESS_CHECKLIST.md`).

## VEGA brain (2026-11-25)
`services/screening/brain.py` covers the uncertainty gates, decision vs outcome quality, versioned knowledge with corrections, counterfactual shadow outcomes, strategy decay, shared risk and the brain's own measures. It is observation only; integrity.py decides its hypotheses (H-ABSTAIN). The gap analysis, the mandatory-test map and what is still missing are in docs/VEGA_BRAIN.md.
