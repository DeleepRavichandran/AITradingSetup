# Learning Brain

A persistent, auditable learning layer on top of the research platform, plus a high-throughput
screening tier for 49,728 strategy configurations, and a benchmark that tests whether learning
actually helps.

**Status, stated plainly: the Learning Brain has NOT demonstrated that memory-informed decisions beat
memory-free decisions on unseen data.** On the one-shot sealed window, no memory arm beat its
memory-free counterpart once the six pre-declared comparisons are adjusted for multiple testing
(smallest Holm-adjusted p = 0.29). The benchmark itself is sound: its negative controls produce no
false positives. Everything below is on the **synthetic** fixture; nothing has been run on real NSE
data yet.

## 1. What was built

```
  research agents (LLM optional)          Learning Brain (no LLM)                    evaluation workers (no LLM, no network)
  ─────────────────────────────          ─────────────────────────────────           ──────────────────────────────────────
  hypotheses, event-engine tests  ──►    episodic memory (append-only,     ◄──      49,728 configurations x every day,
                                         hash-chained, point-in-time)               vectorised, process pool,
                                         semantic memory (findings)                 shared mmap indicator cache,
                                         procedural learning (what to screen)       compute / memory / count budgets
                                         meta-learning (how to rank)
                                         regime eligibility
                                         self-critic (FDR, cost stress, PBO)
                                         system versions: propose → frozen benchmark → forward shadow → HUMAN → champion
                                                                                    ↑ automatic rollback
```

| Requirement | Where | Notes |
|---|---|---|
| 1 Episodic memory | `services/brain/memory.py` | SQLite, append-only (triggers abort UPDATE/DELETE), sha256 hash chain, `available_at` on every row; the research loop now writes every hypothesis here too |
| 2 Semantic memory | `learner.py::findings / recall` | success rate per facet (family, horizon, overlays, sizing) overall and per regime, empirical-Bayes shrinkage, Benjamini-Hochberg across all facets |
| 3 Procedural learning | `learner.py::thompson_scores` | Thompson sampling over facets decides which configurations to screen |
| 4 Meta-learning | `learner.py::model / choose_approach` | calibrated logistic model of P(success); bandit over ranking approaches using each approach's realised forward return |
| 5 Regime adaptation | `regime.py`, `learner.py::eligibility` | point-in-time volatility tercile + trend; families reliably poor in the current regime are benched **for research only** |
| 6 Self-criticism | `critic.py` | BH-FDR over every screened configuration, cost stress, de-duplication, probability of backtest overfitting (CSCV) per batch; `HistoryView` refuses rows after the decision date |
| 7 Controlled self-modification | `versions.py` | a version is a `ResearchPolicy`; protected-settings guard; frozen benchmark; forward shadow evaluation; human-only promotion |
| 8 Continuous evaluation | `versions.py::record_forward / evaluate_readiness` | challenger vs champion only on windows **after** the proposal |
| 9 Rollback | `versions.py::monitor` | failed correctness probe → immediate; reliable forward regression vs the replaced version → automatic |
| 10 Dashboard | `apps/web/app/learning` | KPIs with evidence, forward curves, sealed and negative-control tables, findings, decision changes, versions |
| Distributed evaluation | `services/evaluation/` | 49,728 configurations in 8.7 s on 4 workers (5,745/s) on the fixture; queue backend for multiple machines is **not implemented** |
| Shared data and indicators | `evaluation/cache.py` | built once per dataset hash, memory-mapped read-only by every worker |
| Sealed data | `benchmark.py`, CLI `--final-sealed` | default runs stop at the end of validation; sealed use is a one-shot recorded in the registry and in memory |
| LLM independence | whole `brain` and `evaluation` packages | no LLM calls anywhere in the learning path |
| Immutable audit | episodic memory | every decision, outcome, proposal, promotion and rollback is a hash-chained row |
| No autonomous safety changes | `policy.check_protected`, import test | keys matching risk/limits/broker/credentials/execution/kill/approval/loss/position/… are rejected; brain and workers cannot import execution, risk limits, config, API or agents |

## 2. The strategy universe (screening tier)

5 families × 518 parameter sets × 3 market-regime filters × 4 per-symbol volatility caps × 4 universe
subsets × 2 sizing rules = **49,728** configurations, enumerated deterministically
(`evaluation/space.py`). The vectorised evaluator uses the same timing as the event engine: weights
decided at the close of day t, traded at the open of t+1, flat per-side cost from the dated charge
schedule (13.15 bps on the fixture) plus half-spread.

Agreement with the event-driven backtester (full fixture, same configuration traded through the
real engine with integer shares, dated charges and the ledger):

| Family | Daily return correlation | Annual return gap (screener − engine) |
|---|---:|---:|
| breakout | 0.948 | +1.1% |
| mean reversion | 0.905 | −0.7% |
| momentum | 0.941 | +0.7% |
| RSI | 0.923 | +0.5% |
| trend | 0.958 | +1.9% |
| momentum + all overlays | 0.941 | +0.3% |

The screener is slightly optimistic, mainly because it rebalances to fixed weights for free as
prices drift. It is a screening tier; finalists should be confirmed in the event engine, and any
configuration can now be backtested or paper-traded there as `screened_config` (identical fills to the
screener's weights, tested).

**Context extension (added 2026-11-19).** With a market-context dataset and/or scheduled events, the
universe grows by 518 × 8 filters × 2 volatility caps × 2 universes × 2 sizings = 33,152 to **82,880**.
The filters: India VIX calm, no VIX spike, broad market (sector breadth), stable rupee, calm crude, and
flat over results / macro events / expiry. Base indices are unchanged. `nseql context-test` compares
every filtered configuration with its identical unfiltered twin and against the same filter shifted in
time (a placebo with the same on/off pattern). On the synthetic fixture with random context, no filter
passed in 30 runs; a deliberately leaked filter was detected (z ≈ 9.7). The Learning Brain's decision
loop does not use the context extension yet, and nothing has been run on real context data.

## 3. Learning KPIs

| KPI | Measured as | Target |
|---|---|---|
| Out-of-sample lift | mean forward return per decision window, memory arm minus its memory-free counterpart; block-bootstrap 90% CI; Holm-adjusted p | CI lower bound > 0 |
| Forward Sharpe | stitched forward returns of the selected portfolios | memory ≥ counterpart |
| Calibration | prequential Brier skill of P(success) vs predicting the historical base rate; ECE; AUC | Brier skill > 0 |
| Research efficiency | screen quality: share of screened configurations beating the universe forward median (ties half) | memory > counterpart with CI > 0 |
| Regime robustness | worst mean forward return over regimes with ≥ 3 decisions | memory ≥ counterpart |
| Diversification | effective number of bets among selected configurations | reported |
| Overfitting control | mean probability of backtest overfitting per batch; trials counted | PBO < 0.5 |
| Negative control | same protocol on a zero-drift random walk | 90% CI includes 0 |
| Forward paper trading | champion's selections in a paper session | **not measured**: the paper engine runs library strategies only |

## 4. Protocol and every run

Each decision is made at the close of day d from rows ≤ d and memory available at d; the selected
configurations are scored on the next 10 sessions, which no arm has seen; outcomes enter memory on
the last day of that window (tested: scrambling data after a cut leaves every earlier decision
bit-identical). Every arm screens 2,000 configurations per decision and holds 10. Memory-free arms
screen the same random sample (common random numbers).

The protocol changed twice **after seeing development results**. Both changes are recorded here, and
the sealed window was used only once, after the last change.

**v1 — first development run (36 windows, 2021-12-31 → 2023-05-19).** Memory vs the same-critic
baseline: +0.05% per window, 90% CI −0.19% … +0.30%. Diagnosis: the critic left ~14 of 2,000 survivors,
so critic arms mostly held cash and selection had no room to differ; the meta-model scored worse
than the base rate (Brier skill −0.11, AUC 0.52) yet was used.
*Change:* comparisons without the critic added; every learned component must beat its own random
control (screening) or the base rate (meta-model) on data it had not seen before it is used.

**v2 — development rerun.** Still no reliable lift. Learned screening appeared to work (screen quality
+7 pts, internal z = 12). **The negative control exposed it:** on a pure random walk, memory
"learned" even more strongly (screen quality +15 pts, p ≈ 0.00002). Cause: configurations that are
flat return exactly 0, and the success label counted ties as failures, so the system learned an
artefact of the label (plus genuine but trivial cost avoidance).
*Change:* ties are decided by a fair coin; de-duplication applies to every ranked arm.

**v3 — frozen protocol.**

| Run | Windows | Primary: memory (no critic) vs counterpart | Other notable |
|---|---:|---|---|
| Structured fixture, dev + validation | 36 | +0.003% / window, CI −0.247% … +0.256% | learned screening underperformed its random control (49.0% vs 50.5%); meta-model Brier skill −0.11 |
| Negative control, random walk with costs | 36 | −0.093%, CI −0.220% … +0.035% | no false positive; screening lift z = 0.19 |
| Negative control, random walk, zero costs | 36 | −0.090%, CI −0.241% … +0.054% | no false positive |
| **Structured fixture, sealed window (one shot)** | **15** | **+0.028%, CI −0.255% … +0.289%, Holm p 1.00** | see below |

Sealed window (2023-05-25 → 2023-12-29), all pre-declared comparisons:

| Comparison | Diff / window | 90% CI | p | Holm p |
|---|---:|---|---:|---:|
| memory (critic) vs baseline critic | +0.583% | +0.173% … +0.932% | 0.049 | 0.292 |
| memory (no critic) vs baseline robust — **primary** | +0.028% | −0.255% … +0.289% | 0.862 | 1.000 |
| memory (no critic) vs greedy | +0.099% | −0.164% … +0.343% | 0.539 | 1.000 |
| memory (no critic) vs random | +0.103% | −0.290% … +0.487% | 0.719 | 1.000 |
| screening-only memory vs baseline robust | +0.212% | +0.058% … +0.353% | 0.101 | 0.405 |
| ranking-only memory vs baseline robust | −0.131% | −0.262% … −0.026% | 0.058 | 0.292 |

What this supports: learned *screening* may help (consistent sign in dev and sealed, not significant
after correction); learned *ranking* (meta-model + approach bandit) does not, and is gated off by
the system's own self-validation. What it does not support: any claim that the Brain improves
returns.

What the system did learn as patterns in memory (BH-controlled findings) is real as description of
past outcomes — e.g. mean-reversion and RSI configurations rarely beat the median in low-volatility
up-trends — but those patterns have not translated into better forward selection.

## 5. Why it may not have helped, and what to try next

* **The data.** The synthetic fixture has a single 3-year path and only ~52 decision windows. Rank
  correlation between consecutive windows' configuration returns is weak (+0.13). Real NSE data
  (Kite import) over many more years is the obvious next test.
* **The target.** 10-session forward returns are noisy; a longer horizon or a risk-adjusted target
  may be learnable where this is not.
* **Selection vs screening.** Screening better candidates did not translate into a better top 10,
  because the ranking step (in-sample Sharpe, robust minimum, meta-model) has little forward skill.
* Any further change must be pre-declared and judged on new data: the fixture's sealed window has
  been used.

## 6. Self-modification and safety

* Versions are policies only. The Brain cannot modify code, credentials, broker or execution
  settings, risk limits or safety mechanisms: such proposals are rejected before validation and the
  packages have no import path to them (tested).
* Promotion requires the operator token **and** a named human approver (agent-like names refused),
  and only after the frozen benchmark and ≥ 6 forward windows after the proposal.
* Rollback is automatic on a failed correctness probe (memory chain, screener causality) or a
  reliable forward regression against the version that was replaced.
* The meta-optimiser only perturbs bounded knobs; in the runs above no challenger was promoted.
* Live trading is untouched and disabled.

## 7. Running it

```bash
nseql brain-benchmark --dataset synthetic_v1 --negative-control      # development + validation, plus random-walk control
nseql brain-benchmark --dataset kite_2026 --final-sealed              # ONE SHOT per dataset; refused the second time
nseql brain-verify                                                    # recompute the memory hash chain
```

Dashboard: **Learning Brain** page. API: `/api/brain/summary`, `/api/brain/reports/{id}`,
`/api/brain/verify`; operator-only `POST /api/brain/benchmark`, `/api/brain/versions/propose`,
`/api/brain/versions/{id}/frozen-benchmark`, `/api/brain/versions/{id}/promote`.

On real data the configurations with long look-backs need enough history: with ~220 sessions of 2026
data only short look-backs qualify and only a handful of decision windows exist, so a meaningful
learning test needs several years of history.

## 8. Limitations

* Synthetic data only; no run on real NSE data.
* Screening is vectorised and approximate (see section 2); the event engine is the reference.
* Switching between configurations is assumed costless in the portfolio-of-configurations returns.
* The process pool runs on one machine. A multi-machine queue backend is not implemented, though work
  units are serialisable and workers are stateless.
* Forward paper-trading KPI is not measured yet.
* Self-modification covers policy parameters, not code or models beyond the registered ones.
