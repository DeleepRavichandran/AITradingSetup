# One-minute screening loop

`nseql.screening` (in `services/screening/`) is the first working slice of the multi-horizon
platform. It is a central scheduler that runs a cycle after every minute of the trading session. Each
cycle updates one shared feature pipeline, evaluates the eligible configurations among up to 50,000,
and writes decisions and simulated orders to a forward paper ledger. It covers **intraday** (1m, 5m,
15m) and **swing** (daily) strategies on cash equities. No broker is ever called.

## How a minute is processed

| Step | What happens |
|---|---|
| Ingest | 1-minute bars go into a durable event log keyed by `(symbol, minute)`. Duplicates are ignored. A copy with different values is quarantined (first value wins). Bars for a minute already processed are marked late and not applied. |
| Wait | A minute is processed once its close + `grace_seconds` (5 s by default) has passed. Missed minutes are processed in order (catch-up). Session minutes with no data become `no_data` cycles. |
| Fills | Simulated orders decided earlier fill at the open of the symbol's bar for this minute. Charges come from the dated schedule (MIS intraday, CNC delivery for swing). |
| Features | 1m bars are aggregated into 5m, 15m and daily bars. SMA 5…200, prior 10/20/55-bar high and low, and RSI 2/5/14 are updated only for timeframes whose bar closed, and only for symbols with data. |
| Eligibility | A configuration is evaluated if its timeframe closed and its symbol has a new bar; if it holds a position (stop check every minute against the 1m low); or if it is intraday and holds a position after 15:15 (square-off). No intraday entries after 15:00. |
| Decisions | Evaluated all at once with array operations. The slice emits BUY, EXIT, HOLD and NO TRADE; SELL, ACCUMULATE, REDUCE and REBALANCE are defined for products not yet built. Actionable decisions become simulated orders with deterministic keys. |
| Checkpoint | Engine state is saved every 15 minutes and at the close, by atomic replace. |

**Idempotency.** The engine refuses any minute at or before the last one it processed, so repeating an
event cannot evaluate a configuration twice. Signal and order rows carry deterministic keys and are
inserted with `OR IGNORE`.

**Recovery.** On start, the checkpoint is loaded and every logged minute after it is replayed through the
same deterministic engine.

**Sizing.** Each configuration trades a fixed Rs 1,00,000 in its own sub-account. There is no shared
capital and no volume cap in this loop yet; the event-driven backtester remains the reference for
realistic execution.

## Registry

Per symbol there are 19 rules (5 SMA crosses, 5 breakouts, 9 RSI) × (3 intraday timeframes × 2 stops +
1 daily timeframe × 3 stops), which gives 171 configurations. 300 symbols give 51,300; the first N by id
are registered. Ids are a hash of family, timeframe, parameters, symbol and stop, so the same
configuration has the same id everywhere.

## Correctness evidence (tests/screening)

| Test | What it shows |
|---|---|
| Feature parity | Incremental features equal an independent pandas implementation at every bar close, across all four timeframes, including symbols with missing minutes (>3,000 comparisons). |
| Decision parity | About 17,000 BUY/EXIT decisions from the streaming engine equal a per-configuration reference state machine run on batch features (12 sessions, 342 configurations). This test found a real defect: on flat prices two moving averages are mathematically equal, and rounding flipped the decision. Rule comparisons now require "greater" beyond a relative 1e-9. |
| Duplicates | The same bars delivered again, the same cycle run twice, and an old minute fed to the engine change nothing: no new evaluations, signals, orders or cycles. |
| Crash and restart | A run stopped after minute 1,234 without a final checkpoint, then restarted from the checkpoint with the feed starting from the beginning, gives a ledger and state identical to an uninterrupted run. |
| Gaps, late data, conflicts, kill switch | Gaps become `no_data` minutes. Late and conflicting bars are quarantined. With the kill switch engaged, screening continues and no simulated orders are created. |
| Clock | The background thread runs a cycle after the minute boundary, with no overruns. |

## Benchmark (2026-11-19)

Hardware: Intel Xeon @ 2.10 GHz, 4 logical CPUs, 15.7 GB RAM; Python 3.13.18, NumPy 2.5.5, one process.

Data: **SYNTHETIC** random-walk 1-minute bars for 300 symbols (2% of symbol-minutes missing) over 3
sessions, i.e. 1,125 minutes and about 330,000 bars. Not market data.

Command: `python scripts/bench_screening.py`. Full results:
[benchmarks/screening_2026-11-19.json](benchmarks/screening_2026-11-19.json).

| Configurations | 1,000 | 5,000 | 10,000 | 50,000 |
|---|---:|---:|---:|---:|
| Evaluations (3 sessions) | 287,145 | 1,543,852 | 3,086,817 | 15,648,707 |
| Cycle time p50 / p95 / p99 (ms) | 12 / 37 / 48 | 22 / 45 / 57 | 30 / 54 / 74 | 85 / 162 / 211 |
| Cycle time max (ms) | 58 | 81 | 115 | 421 |
| Engine only p50 / p99 (ms) | 2.9 / 9.6 | 3.7 / 10.3 | 4.6 / 13.2 | 14.6 / 34.1 |
| Cycles over the 60 s budget | 0 | 0 | 0 | 0 |
| Minutes processed / minutes with data | 1,125 / 1,125 | 1,125 / 1,125 | 1,125 / 1,125 | 1,125 / 1,125 |
| Eligible but not evaluated | 0 | 0 | 0 | 0 |
| Peak resident memory (MB) | 275 | 276 | 277 | 291 |
| CPU seconds / wall seconds | 14 / 20 | 17 / 26 | 21 / 33 | 63 / 102 |
| Ledger database after 3 sessions (MB) | 58 | 81 | 110 | 350 |
| Checkpoint (MB) | 2.3 | 2.8 | 3.4 | 8.0 |
| Kill at minute 682 → restart: checkpoint load (s) | 0.013 | 0.016 | 0.022 | 0.019 |
| Ledger identical to an uninterrupted run | yes | yes | yes | yes |

*Cycle time* covers ingesting the minute's bars, evaluating, and writing the ledger. *Engine only* is
the evaluation step.

### Wall-clock run

The 50,000 registry ran on the real clock for 3 minutes, using the background thread and a feed that
delivers each minute's bars at its close. Decisions were written **2.01 s after each minute's close**:
the 2 s grace period plus 5–9 ms of processing. There were no overruns. These were the session's first
minutes, when few timeframes close; the heaviest minute (15:30, when every timeframe closes) appears
only in the replay benchmark, at 421 ms.

### What the numbers mean, and what they do not

- **Headroom.** At 50,000 configurations the worst minute took 0.42 s of a 60 s budget, in one process.
  Parallel or distributed workers are not justified at this load and were not built.
- **Memory** is dominated by the Python runtime and the synthetic stream the benchmark holds in memory.
  The engine state is about 8 MB at 50k (the checkpoint size).
- **Storage grows quickly.** The random walk makes rules flip often: 790,683 actionable signals in 3
  sessions at 50k, about 117 MB of SQLite per session. Real data will differ, but retention or an
  analytical store is needed before running for months.
- **Recovery.** After `SIGKILL`, a restart takes about 0.4–0.7 s, including building the registry. It
  loads the last checkpoint (7 minutes behind the kill point at a 15-minute interval) and replays
  logged minutes as the feed resumes. The final ledger is byte-identical to an uninterrupted run at
  every size.
- **Freshness** in the replay benchmark is processing time only. Freshness against a real feed (feed
  delay, jitter, late bars) is **not measured**, because no licensed real-time feed is connected.
- **Not measured:** multi-process scaling, behaviour under real exchange load, and cloud cost. The
  benchmark ran on one 4-vCPU, 16 GB machine.

## Not in this slice

- A live feed: KiteTicker or polling is **not implemented** and needs a licensed real-time entitlement.
- Positional (weekly/monthly) and long-term investing.
- F&O, ETFs, mutual funds and SIP.
- Shared portfolio capital.
- Short selling.
- Volume caps in the paper loop.
- Use of the Learning Brain's memory in live screening.

See [PLATFORM_V5_STATUS.md](PLATFORM_V5_STATUS.md).
