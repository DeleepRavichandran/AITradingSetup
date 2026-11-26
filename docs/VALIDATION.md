# Validation of the brain itself (phase "Brain validation", 2026-11-26)

This phase corrects how VEGA validates its own intelligence. It also sets up the experiment that decides whether the
added intelligence improves decisions. More intelligence features are not the goal.

## Findings from the independent review, reproduced or disproved

The reproduction scripts are recorded in the milestone report. The regression tests named below fail on the old code and pass on the new.

| # | Finding | Reproduced? | Evidence (old code) | Fix |
|---|---|---|---|---|
| 1 | Study significance used the whole sample, including the holdout | **Yes** | A feature with signal only in development and pure noise in the holdout was accepted in **14 of 20** seeds by the old code (**0 of 20** after the fix, identical data). A first attempt with a weak real holdout signal was a flawed test: the new code correctly found that signal, so the test was corrected rather than the code. | Staged validation (`validation_protocol.py`): development selects, and a sealed holdout is evaluated once per definition, only for development survivors |
| 1b | A candidate could qualify with no holdout evidence | **Yes** (code path) | `holdout_mean is None` passed the verdict check | Missing or small holdout caps the status at "validation candidate" |
| 1c | Events were treated as independent | **Yes** | With no signal and shared session moves, a per-event t-test gave a **41%** false-positive rate at a nominal 5%; session-clustered gave **4%**. Power is kept: a 0.6% session-level effect against a 1% session spread over 30 sessions is found at least 80% of the time (a 0.4% effect only about 62% of the time, by arithmetic) | Session-clustered tests and bootstrap |
| 2 | Playback used information newer than the chosen time | **Yes** | Opportunity (same-day later signals), trading performance (later exits) and methodology (later research runs) changed when later data was added. Portfolio risk used the current positions, and data quality had no time filter | Every map reconstructs from records available at T; arrival time is used for live bars; maps that cannot be reconstructed are marked unavailable in playback |
| 3 | A missing benchmark counted as a 0% benchmark in the promotion gate | **Yes** (code path) | `... if k in bench_daily else 0` | A missing benchmark blocks promotion; comparisons need intervals above zero |
| 3b | Approval was not bound to the exact candidate | **Yes** | Any decision with subject "challenger" counted | Approval is bound to a candidate fingerprint (version, configuration, rules, commit, data and manifest hash, execution version, risk policy, results hash). Any change voids it. The states eligible, approved and deployed are kept separate. |
| 4 | Next-bar fills were not causal | **Yes** | A fill at the open of the next 1-minute bar uses the first price at the instant the decision candle closes; the decision is made 6–55 s later | Execution 2.1: an order fills at the first price observed after the decision (`delay_bars = 1`), plus the modelled spread and impact |
| 5 | The hash chain can be recomputed by a writer | **Yes** (by design) | Rewriting a record and re-chaining passes `verify()` | Role-restricted appends, anchors outside the ledger, verified backups. The residual risk is stated below. |
| 6 (own review) | With few sessions, comparisons read "ahead" on a zero-width interval | **Found in this phase's screenshot review** | With 5 sessions or fewer (the bootstrap block length) every resample is the whole series, so the interval collapsed to the mean. The holdout ablation read "B vs A: ahead (+0.393% .. +0.393%)" on 3 sessions | No reading below 20 sessions in the history comparisons; a promotion benchmark gate cannot pass below 20 sessions |
| 7 (own review) | Variant B could never trade | **Found in the same review** | B combined "cash unless qualified" with "explore at a quarter size". The cash refusal is checked first, so B made 0 round trips and could never earn qualifying evidence | B is "explore at a quarter size" only (bc-1.1). A test asserts that B trades wherever the incumbent does; a variant with fewer than 30 round trips is flagged in the report |
| 8 (own review) | Quarter-size exploration had no effect under the ranked allocator | **Found by the new B0 size-matching test** | The ranked allocator sized orders before the quarter-size scaling was applied, and the scaled size was never read. B's average ticket equalled A's (ratio 1.0), so B was identical to A. Any earlier H1b experiment run with the ranked allocator measured the baseline. The live rules do not use this setting | The scaling now happens before ranking (B's ticket ratio is now 0.23). B0 is matched to B on both trade count and average ticket size, so "smaller" cannot pass for "better" |

## Evidence statuses

The word "validated" is never used for a study result. The statuses are:

| Status | Meaning |
|---|---|
| insufficient evidence | too few development sessions or events |
| rejected | failed development (after correction) or failed the sealed holdout |
| exploratory finding | raw development signal that did not survive correction |
| validation candidate | passed development; holdout missing, too small, or already inspected under other definitions |
| holdout-supported finding | passed development and the sealed holdout, session-clustered and corrected |
| forward-supported finding | also supported by forward shadow-portfolio sessions; set by shadow evaluation, never by event studies |
| promotion-eligible methodology | all promotion gates pass; only the operator approves, and deployment is a separate code change |

## Trust boundary

| Actor | May write |
|---|---|
| research code | results, knowledge, concept registrations |
| evaluation layer | also criteria, protocols, holdout accesses, suspensions |
| operator (API with the operator key) | also decisions and acknowledgements |

No code path lets research change risk limits, the live flag, benchmarks or approvals.

**Residual risk (open).** A process running as the same OS user on the laptop could rewrite the ledger, the anchors and the backups together. Only anchors recorded off the machine (the assistant's daily record) would detect that. Separate OS accounts or write-once storage would close it.

## Brain comparison

The variants are described in `brain_comparison.py`:
- **A** incumbent
- **B** uncertainty-aware
- **B0** matched control (B's trade count and average ticket, with A's ranking)
- **C** market context (supported additions only)
- **D** combined
- **E** references

They run in the weekly historical validation on real minute data, and B also runs forward as a live shadow paper portfolio. A variant may be found worse than A; that is reported as such.

## Positive controls are honest, too

A planted fair-value-gap effect was first built badly in three ways, and each time the study's controls caught it:
1. **Drift.** The planted rises made every day drift upward, so random timing did as well as the pattern. The study rejected it.
2. **Gaps in the rise.** The rising candles themselves formed gaps, so the concept as a whole lost money. The study rejected it.
3. **Pattern-specific.** Built correctly (drift cancelled, overlapping rise candles), the study finds it on development and on the sealed holdout, against zero, the simpler rule and random timing (`test_study_finds_a_planted_effect`).

The controls are strict, but they are not blind.

## Execution 2.1 (causal fills)

| Event | Minute T |
|---|---|
| Candle T closes | T |
| Decision made | T + 6–55 s (measured per minute; above 55 s the minute is catch-up and never trades) |
| Old fill: open of candle T+1 | the price at T, before the decision |
| New fill: open of candle T+2 | the first price at T + 60 s, after the decision and submission |

The modelled spread and impact apply on top. Earlier ledger rows are not rewritten. Each account's `execution.json` records the minute from which 2.1 applies.

The forward challenger contest restarts under 2.1, because its folder is tied to the execution version: comparisons need identical execution on both sides.

## Milestone report: answers to the eight questions (2026-11-26)

**All the evidence in this phase is synthetic or code-path evidence.** The real-market answers come from:
- the laptop's weekly historical validation;
- the forward shadow portfolio B.

Neither has run on this code yet.

1. **Is the brain more reliable?**
   - Its validation is more reliable. Five reproduced defects are closed: leakage from holdout reuse, the independence assumption, playback leakage, the promotion gate, and optimistic fills.
   - Whether its *decisions* are more reliable is not known yet.
2. **Can it detect its own incorrect conclusions?**
   - Partly, and this is now tested:
     - a development-only signal is rejected on the holdout (0 of 20 accepted, against 14 of 20 before);
     - re-reading a holdout under new definitions is detected;
     - a rewritten ledger is caught by its anchors;
     - tampered research verdicts are caught by the consistency check.
   - It cannot detect a same-user rewrite of the ledger, the anchors and the backups together.
3. **Does it identify genuine opportunities better?**
   - Not shown. On planted synthetic effects it finds the real one and rejects noise.
   - On real data, B vs A has not run yet.
4. **Does it avoid weak trades?**
   - Not shown. B0 (B's trade count and ticket size, with A's ranking) exists precisely so that "fewer or smaller trades" cannot be mistaken for "better selection".
5. **Does intelligence improve decisions after costs?**
   - Unknown. No concept or feature is holdout-supported, so C and D have not run.
6. **Which components add no value?**
   - On the evidence so far, none of the concept or feature signals has earned a holdout-supported status.
   - Until one does, they add no decision value. They remain descriptive.
7. **Has any improvement survived independent real-market evaluation?** No.
8. **What evidence would invalidate these conclusions?**
   - A real-data run in which B beats A and B0 on the sealed holdout and forward, with intervals above zero after costs. That would show the brain does select better.
   - Shadow B behind A over 60 or more sessions would show uncertainty-awareness does not help.
   - Any S1 or anchor alarm on the laptop would show that the integrity guarantees have a gap.
