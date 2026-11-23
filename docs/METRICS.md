# What every dashboard number means

One definition per figure (`services/screening/metrics.py`), used by My Trading, Mission Control, Command Center,
the Strategy Monitor and the contest. Pages read these values; they do not recompute them. Regression tests:
`tests/screening/test_metrics.py`.

## Account (paper, the live screening account)

| Figure | Record | Window | Denominator |
|---|---|---|---|
| Account value | engine equity: cash + held shares at the last 1-minute close | now | — |
| P&L today | account value minus the last recorded equity **before** today's first processed minute (or the account's start cash if it has none) | today's IST session | rupees |
| Since inception / since the start | account value minus the account series' **original** capital (`live.json original_capital`; a rules change continues the same money and keeps it) | since the series began | rupees; % = ÷ original capital |
| Exposure | gross value of held shares ÷ account value | now | account value |
| Free cash / in stocks | cash minus cash reserved for working orders / gross held value | now | rupees |

**Corrected (review 2026-11-24).** With ₹1,00,000 original capital, ₹97,752 equity and a flat day, Command Center
showed 0% "since start" because it divided by the continued account's start cash (₹97,751.87). It now shows
P&L today 0 and since inception −₹2,248.13 (−2.25%) (test `test_flat_day_on_a_continued_account_is_not_called_since_start`).

## Session decisions (BUY signals of one IST session)

From the `signals` table, by the **recorded** allocation reason (`alloc_status`, `alloc_reason`). Orders and fills
use the same IST date (`paper_orders.decided_at`, `paper_fills.ts`).

| Figure | Meaning |
|---|---|
| Warm-up history | BUY decisions on historical bars before the account's live start; never tradable. Shown separately, never as "refused by limits" |
| Eligible paper BUY signals | BUY signals at or after the live start: the paper-tradable population |
| ↳ accepted | eligible and given an order |
| ↳ deferred | eligible, but a higher-ranked signal used the room that minute |
| ↳ refused: *category* | eligible and refused, by recorded reason category: daily loss limit; cash / position / exposure limits; churn limits (cool-down, daily caps, one at a time); losing-strategy gates; size or liquidity; kill switch |
| Orders placed | BUY orders decided that session |
| Fills | executions that session (buys + sells) |

**Corrected.** My Trading said skipped buys were "skipped by the safety limits"; the contest labelled 4,68,874 buys
"refused by limits"; the Strategy Monitor's top refusal was "warm-up history (before live start)". All three mixed
warm-up history (never tradable) with real refusals and counted over all time. Every count is now per session (or
per contest session), by recorded reason, with warm-up separate (`test_session_decisions_separate_warm_up_from_eligible_signals`).
Each count on Mission Control and My Trading opens the Strategy Monitor ledger with the same date, decision, status
and reason filters (`/api/screening/signals?day=&reason=&warmup=`).

## Strategy Universe funnel and evidence

| Figure | Population | Window |
|---|---|---|
| Funnel: registered → evaluated (incl. warm-up) → paper BUY signal → allocated capital → completed a trade in this account | this account's configurations, its own ledger | since the account started; warm-up signals excluded |
| Evidence book: configurations with forward evidence, qualified | the allocator's evidence: this account **plus evidence inherited** from earlier accounts of the series | all inherited history |

**Corrected.** The funnel showed 0 "allocated" and 293 "completed a trade": the first counted this account's own
signals, the second the evidence book including inherited round trips. They are different populations and are now
shown separately.

## Other quantities kept separate

- **Registered configurations** (rule × timeframe × stop × symbol of the live account), **independent rules** (the 36
  rules), **benchmark configurations** (the AI Research Brain's research universe) and **agents** are different counts
  and are labelled as such.
- **Contest**: buy decisions are counted within the contest sessions only, with warm-up separate.

## Data gaps

When the API does not provide a field the page shows **Unavailable** (`na()` in `components/ui.tsx`) instead of a
value. Known gaps: the screening account does not record why an accepted entry expired unfilled beyond the
"unfilled after N min" reason; NIFTY is no longer fetched, so market breadth is measured on the watchlist only.
