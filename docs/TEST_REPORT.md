# Test report

Run on 2026-11-17, Python 3.13.18, numpy 2.5.5, pandas 3.1.1, Linux. Command: `pytest`.

| Suite | Tests | Result | Time |
|---|---:|---|---:|
| `tests/unit` | 95 | all passed | 9.6 s |
| `tests/integration` | 59 | all passed | 25.4 s |
| `tests/regression` | 5 | all passed | 3.5 s |
| `tests/acceptance` | 17 | all passed | 24.9 s |
| `tests/audit` | 31 | all passed | 5.0 s |
| **Total** | **207** | **207 passed, 0 failed, 0 skipped** | 53.6 s |

Figures from the run of 2026-11-17 after the audit corrections ([AUDIT_2026-11.md](AUDIT_2026-11.md)).
Both Docker images were built and the API container was smoke-tested; the web container and the
compose stack were not run.

Web: `tsc --noEmit` clean; `next build` succeeds (18 routes). All 18 pages were loaded in
headless Chromium against the live API with zero console errors or failed requests. Driven end
to end in the browser: token login (wrong token rejected), phone viewport (no horizontal
overflow), run a backtest, simulate a portfolio, start a paper session and place an order,
sandbox-test code from the editor, ask the analyst.

Not run: the web container, the compose stack, the GitHub Actions workflow, anything against a real broker, data
vendor, WhatsApp or the Anthropic API.

## What the tests establish

**Accounting (unit, incl. property-based).** Long/short round trips, FIFO lots, flips through
zero, futures daily settlement, option premium flows, splits (including fractional entitlements),
dividends, isolation between strategy books. A Hypothesis test drives 300 random sequences of
fills, marks, settlements, dividends and splits and requires every ledger identity to hold after
every event, then flattens the book and requires equity = cash exactly.

**Charges (unit).** A hand-computed delivery trade to the paisa; STT rate changes on
2023-04-01, 2024-10-01 and 2026-04-01; exchange charge change on 2024-10-01; sell-side-only and
buy-side-only levies; option charges on premium; per-order brokerage caps across partial fills;
refusal of dates before schedule coverage.

**Execution (unit).** Next-bar fills, limit trade-through vs touch, favourable gaps, stop gaps,
stop-limit gaps, OCO resolved against the trader, participation-capped partial fills, zero-volume
bars, adverse tick rounding, circuit locks, fill order, latency.

**Causality (unit).** History visible to a strategy grows one bar at a time; history is a copy;
the portal cannot move backwards; for eight library strategies, scrambling all prices after a
cut date leaves every earlier order unchanged; truncating the data leaves earlier fills unchanged.

**Derivatives (unit, incl. property-based).** Intrinsic-value settlement for calls and puts, long
and short; exercise STT only for long in-the-money positions; futures settlement at the underlying
close; margin for naked, vertical, iron-condor and ratio structures; atomic multi-leg orders;
margin-call liquidation. A Hypothesis test checks option P&L against the textbook payoff for 200
random strike / price / premium / direction combinations.

**Research (integration).** The expression language rejects 11 classes of injection; 12 hostile
code samples are rejected statically and never reach a process; the sandbox child has no secrets,
cannot import the execution package and has no network; a CPU-bound strategy is killed; a
strategy that reads tomorrow's close through engine internals is caught by the look-ahead trap;
the auditor rejects 11 kinds of flawed evidence; the sealed set is single-use and director-only;
the full loop respects budgets and never runs a non-sealed backtest past the validation boundary.

**Paper and execution (integration).** Paper session equals the backtest fill for fill; stale
feed, feed failure and loss limit halt the session and cancel working orders; every live-gate
condition is independently required; tampered, forged and expired approvals are rejected; a broker
adapter sends nothing while the gate is closed; router idempotency, rate limit, value limit,
session hours and circuit breaker.

**Regression.** Exact fixture content hash and exact result hashes of four backtests.

## Defects found by the tests during development (all fixed)

1. A reverse split that left zero shares dropped the lot's remaining entry charges, breaking the
   "charges fully attributed" identity. Found by the property test.
2. The first look-ahead audit (scramble prices after a few cut dates) **passed a strategy that read
   one bar ahead**, because only decisions on the cut bar itself could differ. Replaced by the
   noise-until-revealed trap, which compares every order.
3. The research loop re-proposed the same rule with different starting parameters and counted it
   as new; after the grid search the three "hypotheses" were identical. Duplicate detection now
   ignores the starting value of any tuned parameter, and a newly promoted candidate is added to
   the redundancy check immediately.

## Known weak spots in coverage

- No test uses real market data, so nothing here validates the parsers or the cost model against
  an actual contract note.
- Intraday strategies are tested for mechanics (square-off, MIS rules) but have no golden values.
- The dashboard has browser smoke checks, not a maintained UI test suite.
- Margin is tested against its own specification, not against exchange SPAN figures.
