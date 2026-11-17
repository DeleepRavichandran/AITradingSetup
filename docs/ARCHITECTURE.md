# Architecture

## 1. Shape of the system

```
                    ┌──────────── apps/web (Next.js) ────────────┐
                    │ 18 pages; talks only to /api on its origin │
                    └───────────────────┬────────────────────────┘
                                        │ HTTP + bearer token
                    ┌───────────────────▼────────────────────────┐
                    │ services/api (FastAPI)                     │
                    └─┬─────────┬───────────┬──────────┬─────────┘
       ┌──────────────▼──┐ ┌────▼─────┐ ┌───▼──────┐ ┌─▼───────────────┐
       │ research loop   │ │ run      │ │ paper    │ │ execution       │
       │ + 9 agents      │ │ harness  │ │ session  │ │ gate + router   │
       └───┬─────────┬───┘ └────┬─────┘ └───┬──────┘ └─┬───────────────┘
           │ sandbox │          │           │          │ (closed by default)
           │ (child  │   ┌──────▼───────────▼──────┐   ▼
           │ process)├──►│ backtester engine       │  broker adapters
           └─────────┘   │ exec sim · ledger ·     │
                         │ margin · charges        │
                         └──────┬──────────────────┘
                         ┌──────▼──────────────────┐   ┌──────────────────────┐
                         │ data portal (PIT)       │   │ registry (SQLite)    │
                         │ datasets (Parquet)      │   │ runs · hypotheses ·  │
                         └─────────────────────────┘   │ code · lessons · log │
                                                       └──────────────────────┘
```

One Python distribution, one import namespace (`nseql`), deliberately not microservices. The
service folders are module boundaries; the dependency direction is strictly downward in the
diagram. `nseql.execution` is imported only by the API and refuses to import inside the sandbox.

## 2. The event loop and why it cannot look ahead

For each timestamp on the timeline (`services/backtester/engine.py::step`):

1. session start → corporate actions effective that date
2. **match** orders submitted on *earlier* steps against this step's bars
3. mark positions; margin and daily-loss surveillance
4. from 15:20, queue square-off orders for MIS positions (they trade from the next bar)
5. each strategy's `on_bar` → orders → pre-trade risk → resting queue
6. session end → expiry settlement, futures MTM, DAY-order expiry

Step 2 precedes step 5 and only considers older orders, so an order can never fill on the bar
that produced it: a market order fills at the **next** bar's open. Strategies see data only
through `ctx.history()`, which returns copies of a trailing window ending at the current bar.
A bar's timestamp is its close time (exchange-local IST).

Three independent defences are tested: (a) the API design above; (b) `GuardedPortal`, a
look-ahead trap that replaces every future value with noise until the clock reaches it — a
causal strategy produces identical orders, a peeking one does not; (c) static rejection of
private-attribute access in generated code.

Paper trading calls the same `engine.step`, so a paper session over the same bars reproduces a
backtest fill for fill (asserted in tests).

## 3. Execution model (bars, not ticks)

| Aspect | Rule |
|---|---|
| Market order | next bar open ± (half spread + impact), rounded adversely to the tick |
| Limit order | fills only if price trades **through** the limit (touch is not enough, configurable); gap in your favour fills at the open |
| Stop / stop-limit | fills at the worse of stop and open, plus slippage; stop-limit that gaps past its limit does not fill |
| Stop and target both reachable in one bar | the stop is assumed first (`oco_policy="worst"`) |
| Liquidity | at most `max_participation` (10%) of a bar's volume across all orders in the symbol; remainder stays working; zero-volume bars fill nothing |
| Impact | `impact_coeff_bps × sqrt(qty / bar volume)` |
| Price bands | limit orders outside the band rejected; no buys while locked limit-up, no sells limit-down |
| Multi-leg | all legs fill on the same bar or none do |

Bar data cannot reproduce queue position, intrabar path or tick-level scalping. One-minute bars
are the finest granularity supported and results on them should be read with that in mind.

## 4. Accounting

All money is **integer paise** (`services/backtester/portfolio.py`), so invariants are exact
equalities:

- one `Book` per strategy; a book spends only its own cash; `account cash == Σ book cash == Σ ledger`
- positions are FIFO lots; a `Trade` is a closed lot; charges are apportioned to lots by quantity
- cash equities and options exchange full value; futures exchange nothing on entry, pay daily
  variation in cash, and settle at the underlying's close on expiry
- options settle at intrinsic value of the underlying's close; long ITM options pay exercise STT
- `equity == deposits + realised + unrealised − charges + dividends`, checked after every run
  and after every event in a 300-case property test

Margin (`margin.py`) is an **approximation** of SPAN + exposure: flat rates for futures and naked
short options, and maximum-loss margining for defined-risk option structures. Real SPAN needs
NSE's daily risk-parameter files. A breach queues forced exit orders for the whole book, tradable from the next bar.

## 5. Data

- A dataset version is a folder: Parquet bars per symbol/interval, instrument master with
  listing periods, corporate actions, point-in-time index membership, immutable raw copies, and
  a manifest with a `content_hash`. Writers refuse to overwrite; `verify()` re-hashes.
- `DataPortal` holds the arrays and exposes them only up to its cursor. Split adjustment uses
  only actions already in effect at the cursor.
- The trading calendar for a backtest is derived from the data. No holiday table is shipped.
- `rules/charges_in.json` and `rules/market_structure_in.json` are effective-dated reference
  data with `source` and `verified` per entry. Dates before the charge schedule's coverage are
  refused unless extrapolation is explicitly requested (and then flagged).
- `EventStore` holds timestamped news/earnings/macro items and answers only "as of" queries.

## 6. Research

A hypothesis is a `StrategySpec`: prose hypothesis, a falsification criterion, features, and
entry/exit expressions in a whitelisted expression language. `codegen` compiles it to a Python
module. That source is hashed, statically validated, stored, and only ever executed in
`sandbox_child` — a separate process with no secrets, CPU/memory/output limits and no network.

Partitions are fixed per dataset on first use: development (60%), validation (20%), sealed (20%).

```
market brief (dev only) → family choice (UCB over past outcomes) → spec → code → validation
→ look-ahead trap → dev grid (small) → cost ×0/2/3 · neighbours · stress · baselines
→ audit → [fail: reject] → validation run (params frozen) → bootstrap · deflated Sharpe ·
regimes · walk-forward → audit + risk → [pass] one sealed evaluation → decision → lesson
```

Budgets (iterations, backtests, seconds, sealed evaluations) are hard stops. Sealed results are
excluded from the memory view handed to generating agents. A rejected hypothesis is never
re-tuned: the learning agent may request one *structural* variant per lineage, or changes family.

## 7. Security boundaries

| Boundary | Mechanism |
|---|---|
| Generated code ↔ host | AST allow-list; mandatory private mount/network/PID namespaces with a read-only root, dropped privileges, scrubbed env, rlimits; fails closed |
| Generated code ↔ orders | `nseql.execution` raises ImportError in the sandbox; strategies get only `ctx` |
| Agents ↔ sealed data | registry grants once per hypothesis, to the director only; all else clamped to validation end |
| Anything ↔ live orders | `LiveGate`: env flag (frozen at start) + kill switch + compliance file + HMAC-signed human approval for the exact source hash |
| Browser ↔ API | mandatory bearer token (operator / read-only viewer); the API refuses to start without one; HTTPS via Caddy |
| Secrets | environment only; never logged, never passed to containers that do not need them |

## 8. Deliberate simplifications and the production path

| Now | Production path |
|---|---|
| SQLite registry | PostgreSQL (same schema; `Registry` is the only writer) |
| Parquet files | TimescaleDB or object storage + DuckDB for large intraday history |
| Threads for background jobs | Redis + a worker queue; jobs are already pure functions of config |
| Jobs and paper sessions in memory | persisted session state; they end on API restart today |
| No metrics export | OpenTelemetry traces + Prometheus `/metrics` |
| Single process | API replicas behind the proxy once state is externalised |
