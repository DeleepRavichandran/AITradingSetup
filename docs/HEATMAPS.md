# Heatmap intelligence

**Code**
- `services/screening/heatmaps.py`: the maps.
- `services/screening/feature_study.py`: whether their features predict anything.

**API**
- `GET /api/heatmaps`: the catalogue, availability and the latest feature study.
- `GET /api/heatmaps/{id}?tf=&sector=&at=YYYY-MM-DDTHH:MM`: one map. `at` plays history back.
- `GET /api/research/features`: the latest feature study.

**Dashboard:** Radar → **Heatmap Intelligence**.
- Clickable stock tiles and rows open the decision dossier.
- Inferred-liquidity rows list their levels and link to Chart Analyst with the ICT/SMC overlays.
- Each map shows its source, timestamp and freshness.

**Daily review:** the `features` step.

## What exists, and what does not

| Map | Status | Built from |
|---|---|---|
| Market map | limited | Colour: change over 15m/60m/1d/5d/20d. Tile size: **today's traded value**, because market capitalisation is not stored. Sectors come from a static NSE classification map for the default watchlist. |
| Sector rotation | available | Equal-weight sector return minus the watchlist average, by timeframe. These are not NSE sector indices. |
| Relative strength | available | Stock minus NIFTY 50 when stored, otherwise minus the watchlist average. |
| ICT/SMC inferred liquidity | limited | Active levels from `market_structure.py` by distance band. **Inferred** from candle geometry; potential liquidity, not resting orders. |
| Relative volume by time of day | available | Each half-hour's volume against the same half-hour on earlier sessions. |
| Volatility | available | Daily range per stock, last 10 sessions. |
| Correlation | available | 5-minute return correlation over the last 5 sessions; holdings highlighted. |
| Opportunity quality | available | The best expected edge after costs, as recorded by the allocator at decision time. |
| Portfolio risk | available | Weight, distance to stop, unrealised result, same-sector weight. |
| News and sentiment | limited | Headline counts from the news watcher; tone from the Fundamental Agent lexicon (observation only). |
| Market regime | available | Breadth, median 1-minute move, dispersion, trendiness per session. |
| Trading performance | available | Net result after costs by strategy type and entry hour. |
| Methodology effectiveness | available | Concept study: each concept against zero, the simpler rule, random timing and the holdout. |
| Market-data quality | available | Share of the 375 session minutes with a bar; late and quarantined bars. |
| **Order-book liquidity (history)** | **unavailable** | The feed has candles, not depth, and no depth snapshots are stored. If depth is ever added, resting orders will be labelled as not guaranteed executable. |
| **Order flow (volume delta)** | **unavailable** | Needs tick trades with the aggressor side. Splitting candle volume into buys and sells would be invented. |
| **Options open interest** | **unavailable** | No options chain is connected (needs a licensed F&O source). |

Nothing in these maps fabricates depth, hidden liquidity, institutional orders, volume delta or options positioning.

## Point in time

- **Playback** recomputes a map from rows at or before the chosen time. Tested: rewriting later prices does not change an earlier map.
- **Features** (`feature_study.panel`) are built only from data at or before each decision time. Tested in the same way.

## Do the features predict anything?

At fixed times every 30 minutes, for every watchlist stock, the study computes:

| Feature | What it measures |
|---|---|
| `mom_60` | The stock's own last-hour return. This is the control. |
| `rs_60`, `rs_day` | Relative strength. |
| `sector_rs_60` | Sector rotation. |
| `rel_volume` | Relative volume. |
| `vol_change` | Change in volatility. |
| `liq_above`, `liq_below` | Distance to the nearest inferred liquidity level. |

The outcome is the next 30 minutes' return.

Tests:
- **Cross-sectional rank correlation.** The information coefficient is calculated per time, using non-overlapping times, then averaged and tested.
- **Correction.** Benjamini-Hochberg across features.
- **Incremental value.** The same test with each feature residualised on `mom_60`.
- **Holdout.** The later 40% of sessions.
- **After costs.** Does buying the top third beat doing nothing after a 0.15% round trip?

Verdicts: insufficient sample, no predictive value, no incremental value over momentum, no value after costs, **candidate**.

A candidate still needs forward sessions, the challenger contest and your approval before it may influence a trade.

**Controls (tests):**
- Noise yields no candidate.
- A planted relative-volume signal is found, and unrelated features stay rejected.

Results go to the research store, the evidence ledger (`HM-*`) and the knowledge store. No trading module reads them (test-enforced).

## Playback is point in time (updated 2026-11-26)

Every map rebuilds from records that were **available** at the chosen time.

| Map or input | What playback uses |
|---|---|
| Live bars | only bars that had arrived (the arrival clock is used when it is real; replayed history uses the bar's close time) |
| Decisions | only decisions already made by that minute |
| Positions | rebuilt from fills up to then (stop distance unavailable: stops are not stored per minute) |
| Trading performance | only exits by then |
| Methodology | only research runs completed by then |
| News | only headlines first seen by then (tone unavailable in playback) |
| Data quality | late and quarantined bars counted only once they had arrived |

Corrected candles never overwrite the ledger (re-sent bars go to a conflicts table), so they cannot leak into earlier playback.

A test changes everything observable after the chosen time (prices, decisions, exits, positions, quarantine, research, news) and checks that all 16 maps are unchanged. The feature study now uses the same staged protocol as the concept study, clustered by day.
