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
