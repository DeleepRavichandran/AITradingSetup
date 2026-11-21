# Test report

Run on 2026-11-19, Python 3.13.18, numpy 2.5.5, pandas 3.1.1, Linux. Command: `pytest`.

| Suite | Tests |
|---|---:|
| `tests/unit` | 117 |
| `tests/integration` | 78 |
| `tests/regression` | 5 |
| `tests/acceptance` | 17 |
| `tests/audit` | 31 |
| `tests/brain` | 51 |
| `tests/screening` | 21 |
| **Total, as root** | **320 passed** (295 s) |
| **Total, as an unprivileged user (as on CI)** | 312 passed, 3 skipped (root-only sandbox fallback) (238 s) |

One-minute screening slice (21 tests; 8 added 2026-11-20 for realistic fills and the shared portfolio — volume cap, fill prices, cash identity, limits, refusals, partial fills, daily loss limit, recovery): incremental features equal a pandas reference; ~17,000
streaming decisions equal an independent per-configuration reference; duplicate events, repeated
cycles and old minutes change nothing; crash + restart reproduce the uninterrupted ledger; gaps, late
and conflicting bars; kill switch; the wall-clock thread; the API (replay job, pagination, filters,
viewer role, restart). Frontend: `tsc --noEmit` clean, `next build` succeeds (21 routes), and the
Strategy Monitor page was loaded in headless Chromium against the live API with zero console errors.
Benchmarks: [SCREENING.md](SCREENING.md).

New on 2026-11-19 (40 tests): holiday list and closed-day scheduling, incomplete daily candles, split/bonus
detection and attachment, market-context alignment and Kite import (mocked), the context placebo test
(negative and positive control), scheduled-event blackouts, screened configurations as a strategy and a
paper session, Windows guards.

Compose stack, 2026-11-19 (API image rebuilt from the working tree of this change; web image from `35fd653`,
web code unchanged): `docker compose --env-file .env -f infra/docker-compose.yml up` with the API and web
services (Caddy/TLS profile not run). Through the web container's proxy: no token 401; operator token
200 with live trading false and the sandbox active; the scheduler running; a `screened_config` paper
session created and reconciled; the session restored with the same step count after an API restart.
Found and fixed: without `--env-file .env` Compose does not read the repository-root `.env` and refuses
to start ("NSEQL_API_TOKEN is missing").

Earlier Docker figures (2026-11-18, commit `35fd653`):
Docker, rebuilt 2026-11-18 from commit `35fd653` with `--no-cache` (API 82 s, web 63 s; built through an
HTTPS-only proxy using a temporary copy of each Dockerfile with two lines trusting the proxy certificate).
API container (`--cap-add SYS_ADMIN --security-opt apparmor=unconfined`, as in the compose file):
refuses to start without `NSEQL_API_TOKEN`; anonymous and wrong-token requests get 401; `/docs`,
`/redoc`, `/openapi.json` and their `/api/` forms get 404; viewer token can read but gets 403 on a
write; `live_trading_enabled` is false; sandbox reports network, filesystem and PID namespaces
active; Docker health check healthy; `/api/brain/summary` 200, `/api/brain/verify` memory chain
intact; a proposed policy change to `max_daily_loss_pct` is rejected as a protected setting. A probe
run through the sandbox inside the container ran as uid 65534, had no network, and could not see
the dataset directory. Web container: all 19 page routes return 200 (page render only; it was not
connected to the API container).

Web: `tsc --noEmit` clean; `next build` succeeds (18 routes). All 18 pages were loaded in
headless Chromium against the live API with zero console errors or failed requests. Driven end
to end in the browser: token login (wrong token rejected), phone viewport (no horizontal
overflow), run a backtest, simulate a portfolio, start a paper session and place an order,
sandbox-test code from the editor, ask the analyst.

GitHub Actions (`python` and `web` jobs) pass on `35fd653`.

Not run: the Caddy/TLS profile of the compose stack, anything against a real broker, data
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
