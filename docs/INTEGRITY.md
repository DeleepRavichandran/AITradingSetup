# Evidence integrity: the adversarial layer

**The aim is not to prove the bot works. It is to find out whether it works, under which conditions, and when it must stop.**

- Code: `services/screening/integrity.py`.
- API:
  - `GET /api/integrity`, readable with the viewer key.
  - `POST /api/integrity/decision`, operator key only.
- Dashboard: Research → **Evidence Integrity**.
- Daily review: the `integrity` step runs after each close.

The layer reads the ledgers read-only. It never changes a trading rule, a limit or the live-trading flag. The only action it can take is to tighten. When a predeclared rule fires, it engages the kill switch, which is reduce-only: exits continue and there are no new buys. Only the operator can reset the kill switch.

## How each requirement is met

| # | Requirement | How |
|---|---|---|
| 1 | Independent generation and criticism | Generation stays with the research loop, the Learning Brain and the challenger. Criticism is this separate, deterministic module (`critic`). It takes no AI input. |
| 2 | Evidence for and against every hypothesis | Every hypothesis gets the same contradicting checks: best session removed, best stock removed, costs vs gross edge, market beta (buy and hold, NIFTY 50), regime concentration. |
| 3 | Falsification criteria before the data | `register_seed_hypotheses` writes each hypothesis with its criteria. Only sessions **after** the registration date count; earlier sessions are labelled *exploratory* and decide nothing. |
| 4 | Immutable records | `experiments/integrity/ledger.sqlite` is append-only and hash-chained, and SQLite triggers refuse UPDATE and DELETE. `verify()` finds any rewrite. Failed results are kept like passes. |
| 5 | Untouched out-of-sample | Confirmatory windows only (rule 3). The forward contest uses a manifest frozen at inception. The research registry seals its holdout. |
| 6 | Point-in-time data | Checks on every fill: arrival, decision and earliest-fill times. Late bars are flagged. Catch-up minutes never trade. Rule **S1** suspends trading if any of these is violated. |
| 7 | Multiple-testing correction | Benjamini-Hochberg at α = 0.05 across the hypotheses, and at 10% across every traded configuration with 5 or more round trips. |
| 8 | Randomised and placebo controls | Random entries in the same stock, on the same day, held for the same time and at the same cost: 2,000 seeded draws give a p-value. The Fundamental Agent keeps its own placebo. |
| 9 | Benchmarks | Cash, NIFTY 50 (when stored), buy and hold of the watchlist, and a trend rule (`benchmarks.py`). Promotion must beat all of them as well as the incumbent. |
| 10 | Cost and delay stress | Costs doubled, and entry and exit moved one minute later, both run on the real round trips. |
| 11 | Regime sensitivity | Net result by market regime at entry, taken from the trade journal. |
| 12 | Calibration | Rank correlation between the allocator's score at decision and the realised return. It needs 30 round trips before it means anything. |
| 13 | Drift | Page-Hinkley test on net per round trip (δ = 0.5 sd, λ = 8 sd). Calibrated by simulation: 0.3% false alarms on 200 steady trades, and a 1.5 sd fall is always caught. |
| 14 | Rejected opportunities without hindsight | `opportunities.py` writes to a separate research store. A source scan proves no trading module reads it, or reads this module. |
| 15 | Controls AI cannot override | Limits live in `PortfolioRules`; only operator-key routes change them. A test proves this module cannot set rules or the live flag. |
| 16 | Abstention | Without enough confirmatory evidence the verdict is **abstain**, never "works". |
| 17 | Human approval | Promotion gates end with "operator approval recorded in the evidence ledger" (`POST /api/integrity/decision`). Recording an approval changes nothing by itself: a rule change still ships as reviewed code. |
| 18 | Suspension and rollback | Rules S1–S4 are fixed in code (`SUSPEND`). They are checked every 15 minutes while live, and after each daily review. Each one engages the kill switch. On reset, the current triggers are recorded as acknowledged, and a new trigger engages it again. A rollback of rules is a revert to the previous rules version, made by the operator; the AI does not change trading code on its own. |

## Pre-registered hypotheses (version ei-1.0)

| Id | Claim | Falsified if |
|---|---|---|
| H-EDGE | The incumbent beats cash after costs | After 20 confirmatory sessions, the mean daily return is not above 0 at α (BH-corrected), or the 95% upper bound is below 0 |
| H-INDEX | It beats NIFTY 50 | The daily excess return is not above 0 at α |
| H-SIMPLE | It beats buy and hold of the watchlist | The daily excess return is not above 0 at α |
| H-CHALLENGER | The challenger beats the incumbent | The paired daily difference is not above 0 at α |
| H-SKILL | Its entries beat random entries | Placebo p ≥ α after 30 confirmatory round trips |
| H-BLACKOUT | Entries after 09:46 do better (rules 1.3) | On the challenger (no blackout), with at least 30 confirmatory trips in each group, late entries are not better at α |

## Predeclared suspension rules

- **S1** A fill earlier than its decision allows, or a buy decided on catch-up data, in the current account.
- **S2** The evidence ledger's hash chain is broken.
- **S3** The current account is 6% or more below its own peak.
- **S4** A Page-Hinkley deterioration alarm (at least 30 round trips) while the placebo shows no skill (p > 0.5).

## Bias audit

Each bias is reported with its guard, a deterministic check, and a status: `guarded`, `detected`, `exposed` (a known weakness, stated openly) or `not measurable yet`.

The biases covered are: look-ahead, hindsight, point-in-time, survivorship, selection, recency, data snooping and multiple testing, overfitting, confirmation, anchoring, outcome, overconfidence and calibration, and automation.

Known weaknesses, stated in advance (the laptop's first report will confirm or correct them):
- **Survivorship (exposed).** The watchlist is today's large caps.
- **Recency (expected to be detected).** Rules 1.1–1.4 changed within three sessions.
- **Automation (expected to be detected).** Those rule versions were approved in chat, not in the ledger.

Agreement between AI systems (Claude, ChatGPT or any agent) is never read as evidence.
