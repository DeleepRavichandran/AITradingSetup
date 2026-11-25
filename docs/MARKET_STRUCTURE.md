# Market-structure intelligence: concepts on trial

**The aim is not an ICT bot or an SMC bot.** It is a brain that can recognise these methodologies, state them exactly, and find out whether any of them is useful after costs. Until a concept earns promotion through forward evidence and your approval, it trades nothing. The approved trading methodology is unchanged.

- Code:
  - `services/screening/market_structure.py`: the definitions and detection.
  - `services/screening/concept_study.py`: the evidence.
- API:
  - `GET /api/structure?symbol=&day=&at=&tf=5|15` (annotations as of a time).
  - `GET /api/research/concepts` (the latest study).
- Dashboard:
  - Radar → Chart Analyst: structure layers and the event table, with each pattern's research verdict.
  - Research → Brain Intelligence: "Market-structure concepts on trial".
- Daily review: the `concepts` step. Verdicts go to the evidence ledger and the knowledge store.

## Three kinds of information, never mixed

| Kind | What | Shown as |
|---|---|---|
| Observed | open/high/low/close/volume per candle; previous-day and previous-week levels; opening range; VWAP | candles, solid levels |
| Inferred | every pattern: BOS, CHoCH, MSS, CISD, FVG, order/breaker/mitigation blocks, sweeps, inducement, equal highs/lows, OTE, premium/discount, spring/upthrust, absorption-like, exhaustion-like, PO3, SMT; value area from candle ranges | zones, tags, "inferred" label |
| Validated | only what the study shows after costs, against a simpler rule and random timing; and still "candidate" until forward sessions confirm it | verdict tag per concept |

**Not available from Angel One 1-minute candles, so never drawn or estimated:**
- order book and market depth
- bid-ask spread (the simulation uses a modelled spread)
- tick-by-tick trades
- cumulative volume delta
- footprint charts
- liquidity heat maps
- institutional order flow

A pattern is geometry of candles. It is not evidence of institutional orders, stop clusters or intentions.

## Definitions

All thresholds are in `PARAMS`, fixed before any study. Their hash is registered in the evidence ledger, so later tuning is visible.

### Structure
- **Swing.** A high above the K candles on each side.
  - K = 2 for internal structure; K = 5 for external structure and the dealing range.
  - Observable K candles after it forms. That confirmation lag is recorded.
- **BOS.** A close beyond the last unbroken confirmed swing, in the direction of the current structure.
- **CHoCH.** The first such close against it.
- **MSS.** A CHoCH whose breaking candle is a displacement.
- **Displacement.** Body ≥ 1.5 × the 20-candle average range, with the close in the outer quarter of the candle.
- **CISD.** A close beyond the open of the preceding run of 2 or more opposite candles.

### Imbalance
- **Fair value gap (FVG).**
  - Bullish when low[i] > high[i−2] and the middle candle is green. The zone is [high[i−2], low[i]].
  - Observable at the close of candle i.
  - Liquidity void: an FVG at least one average range tall.
- **Lifecycle.** The first retest is recorded. A close beyond the far edge invalidates the gap, which then becomes an **inverse FVG** in the other direction.
- **Balanced price range.** A bullish and a bearish FVG that overlap within 10 candles.

### Blocks
- **Order block.** At a BOS or CHoCH, the last opposite-coloured candle of the leg. A close through it creates a **breaker** in the other direction.
- **At a CHoCH, breaker vs mitigation block.** Take the last same-direction candle before the failed swing:
  - **breaker** if that swing took the previous swing's liquidity (a lower low or higher high);
  - **mitigation block** if it did not.

### Liquidity
- **Levels:**
  - confirmed swings, internal and external;
  - equal highs and lows (within 0.1%);
  - previous-day and previous-week high and low;
  - opening range (09:15–09:30);
  - prior-session value-area high and low, and the point of control.
- **Sweep.** The wick passes a level and the candle closes back inside.
- **Breakout.** The candle closes beyond the level.
- **False breakout.** A breakout that closes back inside within 3 candles.
- **Inducement.** A sweep of internal liquidity while external liquidity lies beyond.

### Range
- **Premium / discount.** Above or below the midpoint of the external dealing range.
- **OTE.** In an up structure, the first retracement into 62–79% of the last internal up leg (mirror for down).

### Sessions and auction
- **Opening-range break.** The first close beyond the 09:15–09:30 range.
- **VWAP.** Reclaim or loss (session VWAP).
- **Value-area re-entry.** The open is outside the prior session's value area, then a close back inside. This is the auction-theory rotation idea.
- **NSE session windows.** Open drive 09:15–10:00, morning, midday, afternoon, close. These are the kill-zone idea adapted to NSE; every result is split by them.

### Wyckoff and volume (inferred)
- **Spring / upthrust.** A sweep of the bottom or top of a tight 20-candle range (≤ 4 average ranges wide).
- **Absorption-like.** Volume ≥ 2× average on a narrow candle after a move.
- **Exhaustion-like.** Climactic volume (≥ 2.5×) on a wide candle at a 20-candle extreme.

### Descriptive only (never signals)
- **PO3.** Observable only at the session close.
- **Volume profile.** Each bar's volume spread evenly over its range.
- **SMT divergence.** Against NIFTY 50, when its bars are stored.

## How a concept is tested (`concept_study.py`)

1. **Event.** Taken at the candle where the concept first became observable. Detection walks candles in order. A test proves the chart drawn as of a time equals the detector run on data cut at that time, so nothing repaints.
2. **Outcome.** Direction × (close h candles later / open of the next candle − 1) − 0.15% round-trip cost. h is 6 or 12 candles. Intraday outcomes never cross the close.
3. **Compared with:**
   - zero;
   - the **simpler rule of the same kind**, for example a BOS against a plain 20-candle breakout, a sweep against "new 20-candle low and a green close", an FVG retest against "pull back 50% in an uptrend";
   - **random timing** in the same stock and session (seeded placebo).
4. **Statistics:**
   - Benjamini-Hochberg across all concepts.
   - A chronological split: the first 60% of sessions for development, the last 40% as holdout. The holdout must agree.
   - Consistency across stocks.
   - Results by session window and by the previous day's volatility (known at the open).
   - Combinations: with trend alignment, and with premium/discount alignment.
5. **Verdicts:**
   - insufficient sample;
   - no value after costs;
   - no edge over the simpler rule;
   - no edge over random timing;
   - **candidate**.

   There is never "validated". A candidate needs forward sessions after registration, the challenger contest, and your approval.

**Controls (tests):**
- On pure random-walk data no concept becomes a candidate.
- On data with a planted, real FVG continuation effect, the study finds positive value after costs that survives correction.

## Limits

- **Historical data:**
  - Candles are 1-minute and 5-minute, built from Angel One bars.
  - The two-year daily history exists only on the laptop.
  - Results on the synthetic data in this repository say nothing about real markets.
- **Long-only executability:**
  - The paper bot only buys.
  - Short-direction concepts are measured for knowledge, but could not be traded as is.
- **Regimes:** the regime labels are coarse (volatility terciles). Sector rotation and cross-asset inputs need data that is not stored yet.
