# Self-improving strategy loop: plan (agreed 2026-11-23, build after the 15:30 close)

The rule: **it never rewrites itself live.** Every change is one change, tested on unseen data, versioned,
approved by a person, and can be rolled back with one click. Real-money orders stay disabled.

## The loop

| Step | What | Status today (gap analysis) |
|---|---|---|
| 1 | Log every trade **plus the market state around it** | Half: signals, orders, fills, costs and allocation reasons are logged; market context per trade is missing |
| 2 | Measure live results against the backtest, **by market regime** | Missing: live configurations have no matching backtest; regimes only exist in the research benchmark |
| 3 | Diagnose where it bleeds (e.g. high-volatility days with stops too tight) | Basic: proven losers lose capital (negative-expectancy gate); no "why" breakdown |
| 4 | AI proposes a fix (e.g. ATR-based stop) as a **challenger with one change only** | Exists for research policy (`brain/versions.py propose`), not for trading rules |
| 5 | Test the challenger on **unseen data**; most ideas die here | Exists for research policy (frozen benchmark, sealed data) |
| 6 | Promote only if it beats the current version **and the operator signs off** | Exists for research policy (human-only approval) |
| 7 | Deploy small, **shadow mode first**, one-click rollback; v1.0 -> v1.1 -> v1.2 | Exists for research policy (forward shadow, automatic rollback); not for trading rules |

## Standing rule: root cause for every trade (operator, 2026-11-23)

Every completed trade, won or lost, is documented with a root cause (`services/screening/rca.py`, table
`trade_rca`): exit reason, holding time, best and worst move while held, profit before and after charges,
market context at the buy (trade journal), a primary cause and contributing causes in plain words.
Written automatically when the 15:30 bar is processed; shown live on My trading ("Why trades won or lost");
any day on demand with `python -m nseql.cli trade-rca --account <folder> --day YYYY-MM-DD`.
"Recovered after the sale" uses prices after the exit: hindsight for learning, never a trading input.

## Build order

1. **Trade journal with market context** (first, tonight): at every entry and exit store the symbol's ATR
   and realised volatility, trend (price vs SMA50), NIFTY move since the open, market breadth, minute of
   session, news count for the symbol, and a regime label (calm / normal / volatile by volatility
   percentile). Point-in-time only: nothing that was not known at that minute.
2. **Bleed report**: P&L and expectancy by regime x exit reason (stop / rule / 15:15 square-off) x family x
   timeframe, with trade counts and confidence intervals; flags cells that lose significantly.
3. **Challenger lab**: rule-based proposals (later optionally LLM) that change exactly one parameter
   (ATR stop multiple, wider fixed stop, skip a regime, later entry); evaluated on held-out sessions not
   used for the diagnosis; then run as a **shadow configuration** on live prices with its own paper ledger
   and no capital.
4. **Promotion**: challenger must beat the champion on enough shadow trades (minimum count, CI excludes
   zero), then the operator approves on the dashboard; strategy set becomes version 1.1; one-click
   rollback to 1.0; every step in the audit log.
5. Re-use `services/brain/versions.py` lifecycle (proposed -> benchmark -> shadow -> ready -> human approval
   -> champion -> rollback) rather than writing a second one.

Honest limit: steps 2-6 need weeks of real paper trades; one day is too few trades to judge a stop rule.

## Lessons from 2026-11-23 (first live day)

| What went wrong | Cause | Fix / lesson |
|---|---|---|
| Kill switch engaged at start of day | An early voice command engaged it before confirmation existed; it persists by design | Check the status bar before the open; reset only on the Risk page |
| "No market data attached" after setting `.env` | A hidden auto-start copy of the lab held port 8000 | Restart = stop every copy first, then start once |
| Live start errors invisible in `api.log` | Errors were only shown on the dashboard | Fixed: `[live-screening]` lines in the log |
| `angel-verify`: first bar 09:09 | Angel One returns 09:00-09:08 pre-open auction candles | Fixed: bars outside 09:16-15:30 are dropped; stored ones skipped |
| `angel-verify`: 403 "exceeding access rate" | Verifier and running lab shared Angel One's request allowance; adapter treated 403 as a login failure | Fixed: treated as rate limit (back off); run the verifier with the lab stopped |
| CI failed after the news watcher | New route `/news/live` broke a safety test (no route may contain "live") and the full suite was not run before pushing | Renamed to `/news/recent`; **run the full test suite before every push** |
| CI failed once on Ctrl K test | Key pressed before the page was interactive (slow CI) | Test waits for the page and retries |
| `npm` blocked in PowerShell | Windows script policy blocks `npm.ps1` | Use `npm.cmd` |
| Dashboard too technical | Built for analysis first | Added "My trading (simple)" page |
| NIFTY 50 treated as a tradable stock | The index was screened like a stock | Fixed (rules 1.1): indices are never traded |
| TATAMOTORS not found | Tata Motors was demerged; the old symbol is gone | Watchlist now has TMPV (unverified at Angel One from here); the log lists close matches for any symbol not found |
| 708 trades by 12:23, daily loss limit hit | 6,120 independent strategies, fast 1-minute rules, tight stops, no cool-down or caps | Rules 1.1: 15-min cool-down, 1 buy per strategy and 4 per stock per day, one position per stock, strategy-type cost gate; `nseql what-if` to compare; judge on later days |
| After an 11:47 restart the money cards showed a fresh ₹1,00,000 and "no new prices" | On Windows the checkpoint save called fsync on a read-only file, which Windows refuses, so no checkpoint was saved; the restart rebuilt the account by replaying every stored minute | Fixed: fsync on the writing handle (test simulates Windows); cycle errors now go to `api.log`; the reopen path keeps the account's capital |
