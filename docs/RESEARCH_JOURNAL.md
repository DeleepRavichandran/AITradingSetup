# Research journal

Chronological record of experiments, decisions and mistakes. The machine-readable record is the
registry (`experiments/registry.sqlite`); this file is the human summary.

**Every result below is on `synthetic_v1` — generated data. None of it is evidence about NSE.**

---

## 2026-11-16 — Entry 1: baseline library on the fixture

**Tested.** All 16 daily library strategies, default parameters, full 2021-01-01 → 2023-12-29
window, ₹10 lakh each, default costs.

**Why.** Smoke test of the engine across equities, futures and options; establish baselines.

**Result.** All runs completed and reconciled exactly. Equal-weight buy-and-hold: +75%, Sharpe
1.40, max drawdown −9%. Trend/breakout rules had Sharpe 1.5–2.0; mean-reversion rules lost
(RSI −8%, Bollinger −25%, z-score −20%); random entry Sharpe 0.61.

**Why it is unreliable.** The fixture's return model has built-in persistence, so trend-following
is favoured by construction; the drawdown is implausibly small for three years of equities. These
numbers describe the generator, not a market. I subsequently reduced the generator's drift and
range; figures above predate that change and were not re-pinned.

**Next.** Do not tune anything on this data. Use it only to verify plumbing.

## 2026-11-16 — Entry 2: mistakes found while building the research loop

1. **Weak look-ahead test.** My first audit scrambled prices after three cut dates and compared
   earlier orders. A strategy deliberately written to read tomorrow's close *passed*: only its
   decisions on the cut bar could differ, and they happened to match. An audit that passes a known
   cheat is worse than none. Replaced with a trap in which every future value is noise until the
   clock reaches it; the cheat now fails on the first order.
2. **Counting the same idea three times.** The first three-iteration run "promoted" two strategies
   and advanced a third — all three were the same breakout rule, because the template's starting
   parameters differed while the grid search chose identical values. Two sealed evaluations were
   spent on one idea. Fixed the structural hash to ignore tuned parameters' starting values and
   made the redundancy check see candidates promoted earlier in the same run.
3. **Lost charges on a degenerate split** (found by property testing): see TEST_REPORT.

**Lesson.** Each of these produced plausible-looking output. The checks that caught them were
adversarial tests written to make the system fail.

## 2026-11-16 — Entry 3: research run `exp_20261116_144251_6bbc`

Budget: 5 iterations, 500 backtests, 2 sealed evaluations. Used: 3 hypotheses, 184 backtests, 2
sealed evaluations, 91 s. Inventor: hypothesis grammar (no LLM configured). Partition: development
2021-01-01 → 2022-10-18, validation → 2023-05-24, sealed → 2023-12-29.

| Hypothesis | Family | Chosen on dev | Dev Sharpe | Val Sharpe | DSR (trials) | Sealed Sharpe / return | Decision |
|---|---|---|---:|---:|---:|---|---|
| `h111614425101br` close above prior N-day high; exit below prior M-day low; 10% stop | breakout | N=40, M=10 | 1.85 | 1.33 | 0.82 (16) | 3.43 / +27.6%, 22 trades | paper candidate |
| `h111614431914vo` above moving average while realised vol under a cap | volatility | cap 0.30, MA 100 | 2.02 | 1.78 | 0.87 (24) | 2.52 / +26.4%, 10 trades | paper candidate (warning: P&L concentrated in one symbol) |
| `h111614434917vo` breakout after a low-volatility squeeze | volatility | cap 0.22, N=20 | 2.54 | 2.73 | 0.98 (32) | not opened — sealed budget exhausted | awaiting a later run |

Baselines on development data: buy-and-hold Sharpe 1.31, random entry 0.27. Two further
iterations found no untested structure in the breakout and volatility families and were recorded
as "grammar exhausted".

**What was available when.** Hypotheses and parameters used development data only; validation was
run once with parameters frozen; the sealed window was opened once each for the first two.

**Why this is unreliable.**
- Synthetic data with built-in trend persistence: all three are trend-following variants and were
  expected to pass. This run demonstrates the pipeline, nothing more.
- Sealed windows of 157 sessions with 10–22 trades are far too short to distinguish skill from
  luck; a sealed Sharpe of 3.4 on 22 trades should raise suspicion, not confidence.
- The deflated-Sharpe pass mark (0.5) is permissive; at the conventional 0.95 only the third would
  pass.
- The bandit chose three trend-flavoured families in a row, so mean reversion and momentum were not
  tested in this run.

**Next experiment.** Not another iteration on this fixture. Import real NSE data, tighten the
audit thresholds, and re-run from a clean registry so the trial count starts at zero.

## 2026-11-17 — Entry 4: does memory help? (Learning Brain benchmark)

**Question.** Do memory-informed research decisions beat memory-free ones on windows no arm had seen?

**Protocol.** Decide every 10 sessions from information available at the decision; score the next 10
sessions; same screening budget (2,000 of 49,728 configurations) for every arm; primary comparison
pre-declared. Full detail and all numbers: docs/LEARNING_BRAIN.md.

**What happened.** v1 showed no difference, but the critic left almost nothing to select. v2 appeared
to show learned screening working — until the negative control (a pure random walk) showed the same
"learning", even stronger. The cause was my label: ties at zero counted as failures. Fixed in v3.
Under v3, structured data showed no lift; both negative controls showed no false positives; the
one-shot sealed window showed no lift on the primary comparison (+0.03% per window, CI −0.26% … +0.29%)
and nothing significant after Holm adjustment.

**Mistake worth remembering.** An internal z-score of 12 for "learned screening" looked decisive. It
was an artefact. Only the negative control caught it. Every learning claim needs a control where
learning is impossible.

**Why it may be unreliable.** Synthetic data, one path, 52 decision windows; I changed the protocol
twice after seeing development results (recorded); the sealed window is now spent for this dataset.

**Next experiment.** Real NSE history via Kite; pre-declare the comparison; consider a longer or
risk-adjusted forward target; keep both negative controls.
