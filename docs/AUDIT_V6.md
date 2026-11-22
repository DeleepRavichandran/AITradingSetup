# Master Prompt V6: current-state audit and capability matrix

Audit of branch `claude/practical-maxwell-un2bhi` at `c62baa6` (2026-11-22), before any V6 change.
"After" columns are filled in at the end of V6 (see the bottom of this file).

Status words: **working** = implemented and exercised by tests on synthetic or replayed data;
**unverified** = implemented, mock-tested only, never run against the real service; **partial** = some
pieces exist, the user journey is incomplete; **absent** = nothing exists. No item here has been run
on real NSE data (none is licensed in this repository).

## Test baseline (before V6)

| Suite | Result |
|---|---|
| Python (`pytest`, as root) | 356 passed |
| Python (uid 65534) | 349 passed, 3 skipped, 4 env-only failures (CA bundle under /root) |
| Web parser (`node:test`) | 5 passed |
| Web e2e (Playwright Chromium + axe) | 22 passed |

## Capability matrix (before V6)

| Area | Status | Evidence / gap |
|---|---|---|
| Intraday equity (1-minute loop) | working | 19 rule families x 3 intraday timeframes x stops per symbol; realistic fills; ranked allocation |
| Swing equity | working | same loop, 1-day timeframe, CNC costs |
| Scalping (sub-minute) | absent | bar-based only; tick simulation not implemented |
| Positional (weeks-months) | partial | daily backtests of library strategies (trend, momentum_rank); no positional workspace or paper horizon |
| Long-term equity investing | absent | buy_and_hold strategy only; no holdings ledger, no fundamentals |
| Futures | partial | backtester models FUT (margin, roll, expiry settlement) on SYNTHETIC futures; two strategies; no workspace, no real futures data |
| Options | partial | backtester models OPT (exercise/settlement, STT), Black-Scholes greeks + IV, option-chain view on SYNTHETIC chains; no real option data (blocked), no volatility surface |
| Multi-leg / volatility strategies | partial | `option_structure`, `option_calendar_spread` on synthetic data |
| ETFs | partial | instrument kind exists and is charged like equity; no ETF data, no ETF view |
| Mutual funds | absent | no NAV data contract, no fund analytics |
| SIP / step-up / SWP / STP / lump sum | absent | |
| Goal-based planning, XIRR | absent | |
| Bonds, REITs, InvITs | absent | |
| Fundamentals, valuation, earnings | absent | scheduled-event store only (results dates with known-from dates) |
| Sector / macro intelligence | partial | market-context importer (VIX, sectors, USDINR, crude, gold) via Kite, unverified; news headlines (RSS) for reading only |
| Corporate actions | working | split/bonus detection and attachment as new dataset version |
| Portfolio intelligence | partial | shared capital pot, limits, ranked allocation in the minute loop; portfolio simulation of library strategies; no multi-asset portfolio |
| AI research loop + Learning Brain | working (infrastructure) | learning NOT demonstrated (sealed test: no significant lift) |
| Strategy universe exploration | partial | Strategy Monitor lists configurations page by page; no aggregation, map, or dossier |
| Vega | partial | voice/typed commands, safety-gated; answers come from a fixed rule set or the analyst chat; not available on other pages |
| Live data | unverified | Angel One 1-minute polling mock-tested; verification harness not yet run by the operator |
| Live orders | disabled | by design |

Parameter combinations are not counted as independent strategies: the minute loop has **19 rule
families**; its up to 50,000 "configurations" are those families x parameters x timeframes x stops x
symbols. The research library has 21 strategy classes.

## UI findings (before)

Screenshots: `docs/screenshots/v6/before/` (Chromium, dark scheme, 1440/820/390 px).

- Two unrelated visual languages: a light/dark admin template for 19 pages, and a separate neon
  full-screen Command Deck with a large decorative reactor.
- The Command Center is a grid of equal-weight small tiles plus tables; it does not show data
  provenance, market session, worker health, risk status or the screening loop at all.
- Navigation is a flat list of 20 links in 5 groups; there is no persistent platform status (data
  source, market session, emergency stop) outside the Deck.
- Mobile: no horizontal overflow; the menu works; tables scroll inside their frames. Long stat grids.
- Accessibility: axe (serious/critical) clean on /deck and /screening after the previous round; other
  pages were not covered by automated checks.
- No duplicate backend modules found. `components/common.tsx` and `components/ui.tsx` overlap slightly
  (metric tiles), acceptable.

---

# After V6 (2026-11-22)

## What changed

| Stage | Delivered |
|---|---|
| A audit | This file; before screenshots; test baseline |
| B design system | `app/globals.css` tokens (dark-first, light toggle), `components/ui.tsx` (Panel + evidence footer, Metric, StatusGrid, Unavailable, Skeleton, Segmented, Funnel, Spark, windowed VirtualTable), grouped workspace shell with a persistent status strip and one-click Emergency stop |
| C Command Center | `/api/command-center` + rebuilt `/`: lab state, platform status (data, session, broker, workers, sandbox, paper, risk, live orders), strategy, AI and portfolio intelligence, readiness, policy versions; every panel states source, period, update time and data quality |
| D Strategy Universe | `/api/universe/*` + `/universe`: family x timeframe map, validation funnel, return distribution, server-paged windowed table, linkable dossier |
| E Vega | `/api/copilot/ask`: evidence-grounded answers to the V6 question list (with links and what was not read); drawer on every page (Ctrl K); Vega page moved into the shell, reactor replaced by a compact voice-state meter; confirmations, emergency stop, refusals and permissions unchanged |
| F multi-asset | `nseql.analytics.planning` (SIP, step-up, lump sum, SWP, STP, goals, XIRR, historical SIP on NAVs); `nseql.data.funds` (AMFI NAV layouts, store, `nseql import-amfi`); `/planner` and `/funds` workspaces |
| G verification | see below |

## Capability matrix (after)

Only changed rows are listed; everything else is as in the "before" table.

| Area | Before | After |
|---|---|---|
| SIP / step-up SIP / SWP / STP / lump sum | absent | **working** (projections at user-assumed returns, checked against closed forms) |
| Goal-based planning, XIRR | absent | **working** |
| Mutual funds | absent | **unverified**: NAV data contract, importer, analytics and historical SIP work on the documented AMFI layouts; no real AMFI file imported (host unreachable from this environment) |
| Strategy universe exploration | partial | **working**: aggregation, server-side paging, dossier (historical backtest per configuration not linked) |
| Vega | partial | **working**: grounded copilot on every page; voice optional |
| Portfolio intelligence | partial | partial: Command Center shows capital, deployment, drawdown, concentration, accepted/deferred/rejected signals; stress scenarios, margin/leverage across products and multi-asset portfolios are not built |
| Positional, long-term equity, ETF, futures, options workspaces | absent / partial | **unchanged**: not built in V6 (no real data for them; no empty pages added) |
| Fundamentals, macro, sector research | absent | **unchanged** |
| Bonds, REITs, InvITs | absent | **unchanged** |

Workspaces from the V6 list that exist and work: Command Center, Market Intelligence (existing),
Strategy Universe, Intraday & Swing Lab, AI Research Brain, Strategy Factory, Backtesting / Walk-Forward,
Portfolio Intelligence (existing simulation page), Paper Trading, Risk Control, Data Quality, Operations,
Configuration & Audit, Mutual Funds, SIP & Goal Planner, Vega. Not built (no working journey yet):
Long-Term Investment Intelligence, Futures Intelligence, Options Intelligence, ETF Explorer, a separate
Swing/Positional lab.

## Tests (after)

| Suite | Result |
|---|---|
| Python, as root | see TEST_REPORT.md (2026-11-22) |
| Web parser (`node:test`) | 5 / 5 |
| Web e2e (Playwright Chromium, mocked API) | 48 / 48: safety flows, new workspaces, copilot, axe (serious/critical) in light and dark on 6 pages, no horizontal scroll at 360/768/1280 px |
| Visual regression (`npm run test:visual`, opt-in, machine-specific baselines) | 6 / 6; a planted change of 6.29% of pixels was caught |

Accessibility fixes found by axe this round: map-cell text contrast, in-text links distinguished by
underline. Keyboard: skip link, visible focus ring, Ctrl K for Vega, Escape closes it, Enter opens a
table row. Browsers: Chromium only (branded Chrome and Edge not tested).

## Performance (docs/benchmarks/universe_api_2026-11-22.json; SYNTHETIC; in-process timing)

| Configurations | Summary after a new minute | Summary cached | 500-row page | Dossier |
|---:|---:|---:|---:|---:|
| 1,000 | 25 ms | 19 ms | 22 ms | 8 ms |
| 10,000 | 73 ms | 22 ms | 16 ms | 7 ms |
| 50,000 | 263 ms | 36 ms | 29 ms | 11 ms |

The browser never receives more than 500 rows at a time and renders only the visible ones.

## Screenshots

`docs/screenshots/v6/after/` (Chromium, dark, against a real API running a synthetic replay and one
research run): Command Center, Strategy Universe and dossier, Vega, planner, funds, Learning Brain,
Intraday & Swing Lab, at desktop, tablet and phone widths.

## Remaining blockers and next milestones

1. **Real data.** Every strategy figure is from synthetic data. Angel One verification (`nseql
   angel-verify`) and a real AMFI import must be run by the operator.
2. **Futures and options workspaces** need licensed historical contract data (option chains, IV) before
   a volatility surface or options analytics can be honest.
3. **Fundamentals and macro** need a licensed source; nothing is scraped.
4. **Per-configuration historical backtests and walk-forward** should be linked into the dossier
   (the backtester supports `screened_config`; wiring a job per configuration is next).
5. **Portfolio stress scenarios and multi-asset portfolios** (equity + funds) are not built.
6. **Learning**: the infrastructure exists; out-of-sample improvement is still not demonstrated.
