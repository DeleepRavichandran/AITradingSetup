# Historical validation on real NSE minute data

**Question:** are the live rules (and the challenger) any good on months of history they were not designed on?

- **Code:** `services/screening/history_validation.py`
- **Command:** `python -m nseql.cli history-validate [--months 12] [--skip-import]`
- **API:**
  - `GET /api/history`: readable with the viewer key.
  - `POST /api/history/run`: operator key only.
- **Dashboard:** Research → **Historical Validation**.

## How it runs, without you doing anything

- **When:** on the laptop (where the Angel One login lives), once a week, after the daily review has finished. It never starts during market hours.
- **How:** it runs in a separate low-priority process.
  1. It imports 12 months of Angel One 1-minute candles for the watchlist and NIFTY 50 (30-day requests, retries built in).
  2. It replays the **exact live rules** and the **challenger**, minute by minute, with the live execution model: next-minute fills, spread, impact, liquidity cap and dated NSE charges.
- **Duration:** a few hours. If it is still running at 09:00, it pauses between days and resumes after 15:35, so the live bot keeps the machine.

## Protocol

The protocol is registered once in the evidence ledger. The holdout only grows forward; it is never re-split.

| Segment | Sessions | Purpose |
|---|---|---|
| warm-up | first 5 | indicators build up; no orders |
| development | first 50% before the live account | reference |
| validation | next 25% | reference |
| holdout | from the registered cut-off; new data joins it | the evidence |
| contaminated | from 2026-11-23 (first live paper day) | rules 1.1–1.4 were shaped on these days, so they are shown but never counted |

No parameter is fitted on this data.

## What is measured

**For each segment**, for the incumbent and the challenger, after all costs:
- total return;
- mean daily return with a 95% block-bootstrap interval;
- maximum drawdown, Sharpe ratio and the worst day.

**Against benchmarks.** Each is compared with cash, NIFTY 50 and buy-and-hold of the watchlist, and the challenger with the incumbent. Each comparison reads as "ahead", "behind" or "indistinguishable" (from the interval).

**On holdout trades:**
- a random-entry placebo;
- doubled costs;
- a one-minute delay.

**Same data, other studies:** the concept study (ICT/SMC/price action) and the feature study (heatmap features).

Results go to `experiments/history/<run>/report.json`, the evidence ledger (`HIST-incumbent`, `HIST-challenger`) and the knowledge store. A historical result never promotes a rule by itself: it adds evidence for the integrity layer and your decision.

## Limits

- **Fills:** candle-based fills with modelled costs; real fills can be worse.
- **Survivorship:** only the current watchlist, so stocks that left the large caps are absent.
- **Corporate actions:** no adjustment is applied to minute data.
- **Coverage:** whatever Angel One returns decides the period. Coverage is shown per symbol, and gaps are never filled.
