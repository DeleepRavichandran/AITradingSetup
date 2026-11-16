# Risk policy

This policy is enforced in code. Where it says "must", there is a check and a test.

## 1. Operating mode

The platform operates in research and paper-trading mode. `LIVE_TRADING_ENABLED` defaults to
false, is read once at process start into an immutable settings object, and has no setter, no
API endpoint and no agent tool. Real-money trading is out of scope until every item in
[LIVE_READINESS_CHECKLIST.md](LIVE_READINESS_CHECKLIST.md) is complete.

## 2. Controls on every order (backtest, paper and live alike)

| Control | Default | Where |
|---|---|---|
| Instrument must be tradable and listed on the order date | always | engine `_validate` |
| Quantity is a positive multiple of the lot size; prices on the tick grid | always | engine |
| Limit price inside the daily price band | always | engine |
| Delivery sells need holdings; cash-market shorts are intraday only | on | `RiskLimits.allow_equity_short_overnight=False` |
| Buying power / margin, checked when the order is placed **and** again at the fill price | always | `margin.required` |
| Max order value | ₹5 crore (backtest), ₹5 lakh (router) | `RiskLimits`, `RouterLimits` |
| Max position value per symbol | ₹10 crore | `RiskLimits` |
| Max gross leverage | 5× | `RiskLimits` |
| Daily loss halt per strategy book | off unless set per run | `RiskLimits.max_daily_loss_pct` |
| Order rate | 5 per second (router) | `RouterLimits` |
| Duplicate order protection | client order id is idempotent | `OrderRouter` |
| Exchange session hours | enforced on the router | `TradingCalendar` |

A margin breach on a book holding derivatives, intraday or short positions cancels its working
orders and liquidates it at the bar's close with slippage; the loss is real and can exceed the
margin posted.

## 3. Capital segregation

Each strategy trades from its own book. A book can spend only its own cash; the account's cash is
the sum of the books. Two strategies cannot fund positions from the same rupee.

## 4. Kill switch

Engaging it blocks every new order and cancels working orders in every paper session. It engages:

- manually, by an operator (name and reason recorded);
- automatically on a stale market-data feed, a feed failure, a session loss beyond the limit
  (default 5% of session capital), a failed ledger reconciliation, or five consecutive rejected
  orders at the router.

Resetting it requires a named human and a reason; names beginning `agent`, `ai` or `system` are
refused. Halted sessions stay halted. Positions are **not** closed automatically on a halt:
closing into a broken feed is its own risk, so that decision is the operator's.

## 5. Research integrity

- A strategy is judged on cost-inclusive, risk-adjusted, out-of-sample evidence. Total profit is
  never a ranking criterion.
- Parameters are chosen on development data only. Validation is used once with parameters frozen.
  The sealed window is opened once per hypothesis, only after the audit passes, only by the
  research director; every grant and denial is in the audit log.
- Every backtest on non-sealed data counts as a trial and deflates later significance tests.
- A rejected hypothesis is not re-tuned. One structural variant per lineage is allowed.
- Auditor thresholds (`agents/team.py::AuditPolicy`): ≥ 25 development and ≥ 6 validation trades;
  development Sharpe ≥ 0.3 and ≤ 4.0; survives 2× costs; ≥ 60% of neighbouring parameter sets
  profitable; beats random entry; validation Sharpe ≥ 0.2 and ≥ 25% of development; bootstrap
  p ≤ 0.15; deflated Sharpe ≥ 0.5; walk-forward out-of-sample Sharpe > 0; the look-ahead trap must
  pass. These are deliberately permissive defaults for a small synthetic sample and **must be
  tightened** (notably deflated Sharpe toward 0.95) before real capital is contemplated.
- Portfolio review: max drawdown ≤ 25%, average gross leverage ≤ 1.5×, daily-return correlation
  with an existing candidate ≤ 0.9.

## 6. Generated code

Runs only in the sandbox. May import only `math`, `numpy`, `nseql.indicators` and
`nseql.strategies.api`. No file, network, process, reflection or private-attribute access. No
secrets are present in its process. It is never imported by the API or execution service.

## 7. Secrets and access

Secrets live in environment variables or a secret manager — never in the repository, logs, the
registry or a prompt. `HUMAN_APPROVAL_SECRET` is held by a person and is not given to the API
container. The dashboard requires a token whenever it is reachable from another machine.

## 8. Statements the platform will not make

It does not predict returns, does not describe any strategy as profitable or safe, and does not
give personalised investment advice. Every report on synthetic data says so.
