# Assistant handover: how to run and watch the lab for the operator

Read this first in every new session. The operator wants ONE assistant that runs, watches and improves the lab, and
answers in chat with real numbers. They do not want to run commands, restart things or send screenshots.

## The objective (read docs/VISION.md first)
One evidence-driven trading platform for sustainable risk-adjusted returns after costs, across intraday, swing,
positional and long-term horizons, equities, ETFs, mutual funds and SIP (and later F&O). It is **not** a strategy factory:
strategy count is not a success measure. Work follows the roadmap in docs/VISION.md:
- every decision recorded, including rejected and missed ones (hindsight only in research)
- benchmarks always shown: cash, NIFTY, buy-and-hold, a simple trend rule, the incumbent
- no rule change after a single losing day
- validated evidence plus operator approval before any promotion

## The setup (done on 2026-11-25)
- The lab runs on the operator's Windows laptop (`C:\Users\delee\AITradingSetup`, branch `feature/core-correctness`).
  It is always on (sleep disabled).
- **Live link:** `https://deleep.taile79d04.ts.net` (Tailscale Funnel to the lab API on 127.0.0.1:8000; the dashboard
  is not exposed).
- **Key:** a read-only viewer key (`NSEQL_VIEWER_TOKEN` in the laptop's `.env`). It is stored in this cloud
  environment's Network secrets for the host above (header `Authorization: Bearer …`), so requests to that host are
  authenticated automatically. Never ask for the key in chat. The viewer key can only read: writes are refused and audited.
- **Daily self-update:** scheduled task "NSE AI Quant Lab daily update", weekdays 16:10
  (`scripts/windows/daily_update.ps1`). It waits for the evening review, fast-forwards to the latest pushed commit of the
  branch, rebuilds, restarts, health-checks and rolls back if unhealthy. The log is `experiments/logs/update.log` on the laptop.
  So: test fully, then push to `feature/core-correctness`. The laptop installs it after the close.

## How to answer the operator
- "How is it going / account / positions / today's trades / price of X / is the feed OK?" Call
  `GET https://deleep.taile79d04.ts.net/api/assistant/status`. It returns:
  - market, bot state and account (value, P&L today and since the start, cash, exposure)
  - positions, today's trades, and the latest price and change of every watchlist stock
  - feed freshness: minutes today by mode (live / catch-up), the last 10 minutes' lags, and coverage
  - the challenger's account
  - the latest evening review: headline plus the trade report
- **"How was today?"** (after about 15:45): `latest_review.trade_report` in the status. It lists every trade, why it
  won or lost, and the "next time" changes. More detail is under `/api/review` and
  `/api/review/{account}/{day}`. Chart read for a stock (trend 5m/15m/daily, support/resistance,
  RSI/MACD/Bollinger, patterns, plain read, the bot's trades): `/api/screening/chart?symbol=INFY` (add `&at=10:30` to see the
  chart as it stood then). Candles for price action: `/api/screening/bars?symbol=INFY&tf=5` (1, 5 or 15 minutes,
  with the candle pattern as of each candle). Every buy's chart picture at entry is in the trade report (`chart_at_buy`).
  Also useful: `/api/today`, `/api/contest`, `/api/fundamental`, `/api/screening/status`.
- Answer in plain words, with real numbers only. If the link or the lab is down, say so; never guess. The operator
  writes in English and Tamil ("machi").

## Standing rules (from the operator; never change these)
- Paper trading only: `LIVE_TRADING_ENABLED=false`. No agent may change it or authorise itself.
- Never change broker credentials, execution permissions, risk limits (daily loss cap, position sizes) or the kill switch
  automatically.
- **Daily trade limit (operator's rule, rules 1.4): at most 10 new trades (buys) a day.** More only with the operator's
  approval and a strong written reason: they enter it on Risk Control ("Allow N more today"), which posts to
  `/api/risk/extra-trades` with the operator key. The assistant's read-only key cannot approve. When the limit is hit
  and more trades look justified, explain the evidence in chat and let the operator decide. Exits are never blocked.
- Never fabricate market data, fills, results or API success. Never claim tests ran if they did not.
- No look-ahead: rank and decide only on information available at decision time.
- Do not merge to main automatically. Never log or commit MPINs, TOTP seeds or tokens.
- Run the full test suite before every push (`.venv/bin/python -m pytest`; the web app: `npm run build` and
  `npm run test:e2e` in `apps/web`).
- Strategy improvements: test each on past days (`nseql experiment`, the daily review's "next time" list), apply what
  beats the current rules, and tell the operator what changed and why. They asked the assistant to decide and act,
  not to offer options.

## Where things stand (2026-11-25)
- **Account:** from 27 Nov `live_angelone_100000_r2_v14` (rules 1.4: no buys before 09:46, at most 10 buys a day),
  continuing `..._v12` (rules 1.2), continued from ₹1,00,000. 23 Nov: −₹2,248 (363 round
  trips). 25 Nov: −₹342 (28 round trips) in a rising market. Since the start: −₹2,590.
- **Challenger:** `..._challenger_orb30-breadth-1_0_x20`, contest from 26 Nov. It needs 60 sessions and 100 trades;
  promotion is the operator's decision.
- **Feed:** Angel One candles sometimes arrive a few seconds late. Since commit 6735409 the loop retries within the
  minute. Check `feed.minutes_today_by_mode` to confirm that catch-up minutes dropped.
- **Next decision:** the operator agreed to move away from near-scalping. Candidates, to test, then apply:
  - drop the 1-minute strategies (keep 5 and 15 minutes)
  - no entries in the first 30 minutes (H2)
  - see `docs/REVIEW_1376b45.md` for the experiment results
- **Fundamental Agent:** observation only until its placebo test passes (`docs/FUNDAMENTALS.md`). Company results
  need the operator's quarterly CSV import.
- Pending: the NSE 2026 holiday list (`nseql holidays`), and India VIX via `import-context` (optional).

## Key documents
`docs/CONTEST.md` (execution 2.0, evidence rules, experiment), `docs/REVIEW_1376b45.md` (review, scores, verdict),
`docs/FUNDAMENTALS.md`, `docs/METRICS.md`, `docs/WINDOWS.md`.
