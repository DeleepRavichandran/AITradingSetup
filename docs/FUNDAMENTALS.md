# Fundamental Analysis Agent

`agents/fundamental.py` looks beyond the chart. For every watchlist stock, and for the market, it scores five lenses.
Each score comes with its evidence and the time that evidence became public.

| Lens | What it reads | Score |
|---|---|---|
| Company reports | the latest quarterly result vs the same quarter a year earlier: revenue and profit growth, operating-margin change, debt/equity | -1 .. +1 |
| News tone | headlines about the stock first seen by the lab in the last 3 days, scored with a transparent finance word list; attention vs the stock's usual count | -1 .. +1 |
| Economic events | results due, RBI policy, Union Budget, FOMC and F&O expiry in the next 3 days, once each was public | risk 0 .. 1 |
| Crowd emotion | market mood from India VIX vs its last year, breadth, panic selling (a big fall on heavy volume), euphoric buying, stocks crowding at 52-week highs, herding (stocks moving together) | fear -1 .. greed +1 |
| Behaviour | anchoring near a 52-week high or low, an overreaction day, a gap that faded, drift after a results reaction, 20-day momentum | -1 .. +1 |

**About "fear" and "overconfidence":** these cannot be observed directly. The agent uses documented proxies with fixed
thresholds (`THRESH` in the code), which were not fitted to any data.

**Observation only.** The agent never places, sizes or blocks a trade. A lens can be proposed as a filter only after it
passes the placebo test, and then only through the experiment protocol, with your decision.

**Never invented.** A missing input makes its lens *unavailable* and is listed under "Missing inputs". The agent never
fills a gap by guessing, and nothing is read before the time it became public.

## Where it shows up
- **Research → Fundamental Agent:** market mood, the placebo-test status, and each stock's evidence.
- **Monitor → Daily Review:** after every close, the agent's snapshot is taken (as of 15:35) and summarised. These
  daily snapshots are the evidence its placebo test uses.
- **Command line, from the lab folder:**

```
.venv\Scripts\python.exe -m nseql.cli fundamental-agent
```
```
.venv\Scripts\python.exe -m nseql.cli fundamental-agent --symbol INFY
```
```
.venv\Scripts\python.exe -m nseql.cli fundamental-test
```

## Company results: once a quarter, about 10 minutes

Angel One and Kite do not provide financial statements. You download them yourself, for free, and import them.

1. Copy `docs/templates/financials_template.csv`, for example to `C:\Users\delee\results_2026Q3.csv`.
2. For each watchlist stock, open its page on Screener.in ("Quarterly Results") or its result filing on NSE (Corporate
   filings → Financial results). Add one row per quarter: the latest quarter **and** the same quarter a year earlier,
   because growth is measured year on year.

   | Column | Value |
   |---|---|
   | `symbol` | the NSE symbol, as in the watchlist (e.g. `INFY`) |
   | `period_end` | the quarter's last day, e.g. `2026-09-30` |
   | `published_at` | **when the result was filed with the exchange** (date and time, IST), from the NSE/BSE announcement. Required: without it the agent could read a result before it was public, so such a row is refused. |
   | `revenue_cr`, `operating_profit_cr`, `net_profit_cr`, `total_debt_cr`, `equity_cr` | in Rs crore |
   | `eps` | in Rs |
   | `source` | e.g. `Screener.in` or `NSE filing` |

   Leave a cell blank if you don't have the figure. Never write 0 for unknown.
3. Import the file:

```
.venv\Scripts\python.exe -m nseql.cli financials-import C:\Users\delee\results_2026Q3.csv --source "Screener.in export"
```

Re-importing a quarter corrects its figures. A correction never makes a result public earlier than its original filing.

## Optional: India VIX (the fear gauge)

VIX comes from Kite's market context (`import-context`). Without it, the crowd lens uses breadth, panic and euphoria
only, and says so.

## Has it earned a say? (the placebo test)

Each paper round trip is matched with the agent's snapshot from the close **before** its entry. For each lens, the test
compares the average net P&L per trade when the score was positive with when it was negative. The same comparison is
then repeated with the snapshots shifted in time: a placebo that keeps how often a score is positive but breaks its
link to the trade.

A lens is **informative** only if the real difference beats the 95th percentile of the placebo, with at least 100 trades
and 20 snapshot days. Otherwise the verdict is "not enough evidence" or "no usable information". An informative lens
becomes a *candidate filter*, to be tested in `nseql experiment` before anything changes.
