# Live readiness checklist

**Current state: NOT READY. Live order submission is disabled and must stay disabled.**

Regulatory and broker requirements for retail algorithmic trading in India change. The items
below are what must be *established and evidenced*, not a statement of the current rules.
Confirm each against the SEBI and NSE circulars and your broker's terms in force on the day.

Legend: ☐ open · ☑ done · ✗ blocked

## A. Data
- ✗ Licensed real NSE history imported and quality-checked (acceptance test 1)
- ☐ Corporate actions supplied and reconciled against a second source
- ☐ Exchange holiday calendar supplied for the trading year
- ☐ Charge schedule entries used by the strategy verified against primary sources
- ✗ Authorised real-time feed connected; stale-feed alarm exercised on it (acceptance test 13)

## B. Strategy evidence
- ☐ Hypothesis re-run from scratch on real data — results on the synthetic fixture do not count
- ☐ Auditor thresholds tightened (see RISK_POLICY §5) and passed
- ☐ Sealed out-of-sample evaluation passed, once
- ☐ At least 60 trading sessions of paper trading on a live feed, with drift inside tolerance
- ☐ Backtest-versus-paper fill comparison reviewed (slippage model calibrated to observed fills)
- ☐ Capacity estimate comfortably above intended capital

## C. Broker integration
- ☐ Broker adapter verified against the real API in the broker's sandbox or with minimum size
  (the Zerodha adapter is mock-tested only; Upstox and Angel One are not written)
- ☐ Instrument master synchronised daily; symbol, lot size and tick mapping verified
- ☐ Daily login / token procedure documented (tokens expire each day and need a person)
- ☐ Order lifecycle handled for every broker status, including partial fills and rejections
- ☐ Position and order reconciliation against the broker at start, intraday and end of day
- ☐ Reconnect and resume tested: process restart with open orders and positions

## D. Compliance (mirrors `experiments/compliance.json`, checked by the live gate)
- ☐ `broker_api_terms_accepted`
- ☐ `algo_registered` — registration / approval of the algo with broker and exchange where required
- ☐ `static_ip_registered`
- ☐ `order_rate_within_threshold`
- ☐ `risk_limits_reviewed`
- ☐ `reconciliation_tested`
- ☐ `recovery_runbook_tested`
- ☐ `tax_and_audit_trail`

## E. Operations
- ☐ Deployed on a host with a static IP, time sync, monitoring and alert delivery tested
- ☐ Registry and experiments directory backed up; restore rehearsed
- ☐ Kill-switch drill performed by the person who will be on call
- ☐ Runbook for: feed loss, broker outage, rejected-order storm, runaway position, wrong fill
- ☑ Paper sessions and the kill switch persist across restarts (tested)

## F. Authorisation — human only
1. Complete A–E and set every item in `experiments/compliance.json` to `true`.
2. A named person runs `scripts/approve_strategy.py` at a terminal holding
   `HUMAN_APPROVAL_SECRET`, for the exact source hash, parameters, capital cap and expiry.
3. The operator starts the execution service with `LIVE_TRADING_ENABLED=true` and the secret
   available to it for signature verification.
4. Begin at minimum size. Any code or parameter change invalidates the approval.

No agent, API call or dashboard action can perform steps 1–3.
