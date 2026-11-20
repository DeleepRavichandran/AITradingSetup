# NSE AI Quant Lab

An autonomous quantitative research and paper-trading platform for India's National Stock
Exchange: an event-driven backtester with an exact cash ledger, a team of research agents that
propose, code, test and criticise strategies, and a web dashboard over all of it.

**Research and paper trading only.** Live order routing is built but disabled, and cannot be
enabled by any agent or through the API. Nothing here predicts or promises profit.

> The repository ships with a **synthetic** fixture market (symbols prefixed `SYN`). It proves
> the machinery works. It says nothing about real markets. To research real instruments you
> must import data you are licensed to use — see [docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md).

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
nseql fixtures                                   # generate the synthetic dataset
nseql backtest sma_cross --end 2023-05-24 --trades
nseql research --iterations 3                    # hypothesis -> code -> sandbox -> audit -> decision
pytest -q                                        # 315 tests (the sandbox tests need Linux)
```

Dashboard (two terminals):

```bash
export NSEQL_ALLOW_INSECURE_LOCAL=true           # this machine only; servers need NSEQL_API_TOKEN
nseql serve                                      # API on http://127.0.0.1:8000
cd apps/web && npm install && npm run dev        # UI on http://localhost:3000
```

To reach the dashboard from anywhere (phone included), deploy the Docker stack behind HTTPS:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). On a Windows laptop: [docs/WINDOWS.md](docs/WINDOWS.md).

Getting ready for real data (each step needs your Kite Connect subscription or files you hold):

```bash
nseql kite-login                                   # once a day
nseql import-kite kite_2026 --symbols RELIANCE,TCS,INFY --start 2024-01-01 --licence "Kite Connect, personal research"
nseql corporate-actions kite_2026                  # unlisted splits/bonuses; attach confirmed ones with --file
nseql holidays nse_holidays_2026.csv --source "NSE circular ..."
nseql events --file results_and_macro.csv --source "exchange filings; RBI/Fed calendars" --expiries 2024-01-01 2026-12-31
nseql import-context --start 2023-01-01 --licence "Kite Connect, personal research"   # VIX, sectors, rupee, crude, gold
nseql context-test --dataset kite_2026             # does any context filter beat a placebo?
```

Paper-trade a screener pick: start a session with strategy `screened_config` and params `{"key": "<16-char key>"}`.

## What works today

| Area | Status |
|---|---|
| Event-driven backtester: equities, futures (daily MTM, expiry, rolls), options (own premium history, expiry settlement), multi-leg, margin, corporate actions, dated statutory charges | Implemented, tested |
| Declarative strategy specs → generated Python → static validation → sandboxed execution | Implemented, tested |
| Nine research agents, budgeted research loop, experiment memory, single-use sealed out-of-sample set | Implemented, tested |
| Walk-forward, bootstrap, Monte Carlo, deflated Sharpe, FDR control, cost / parameter / stress sensitivity | Implemented, tested |
| Portfolio simulation on one shared ledger | Implemented (library strategies) |
| Paper trading on replayed bars, kill switch, safety halts, reconciliation | Implemented, tested |
| Learning Brain: memory, procedural and meta-learning, self-criticism, versioned self-modification with human promotion and rollback; 49,728-configuration screening (82,880 with context filters) | Implemented — **has not yet shown an out-of-sample improvement** (see docs/LEARNING_BRAIN.md) |
| 19-page dashboard on live API data | Implemented |
| Import of real NSE data | **Blocked** — needs files/credentials you are licensed to use |
| Live market-data stream | **Blocked / not implemented** |
| Market context (India VIX, sectors, rupee, crude, gold), scheduled events (results, RBI, Budget, Fed, expiry), placebo test for context filters | Implemented, tested on synthetic data — **not yet run on real data** |
| NSE holiday list, split/bonus detection, paper trading of screener picks | Implemented, tested |
| One-minute screening loop: intraday + swing, up to 50,000 configurations, idempotent, crash recovery, volume-capped fills with spread/impact, one shared capital pot with exposure and daily-loss limits, Strategy Monitor page | Implemented, tested on a **replayed / synthetic** feed (docs/SCREENING.md); live 1-minute feed **not implemented** |
| Kite adapter, WhatsApp alerts, LLM-backed agents | **Unverified** — mock-tested, need your credentials |
| Upstox, Angel One, currency / commodity / rate derivatives, tick data | **Not implemented** |

The authoritative list, with limitations, is [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).
The running system reports the same statuses on its Command Center page.

## Repository layout

```
apps/web/              Next.js dashboard
services/api/          FastAPI backend (the only thing the dashboard talks to)
services/data/         dataset store, fixtures, quality checks, adapters, event store
services/backtester/   engine, execution simulator, ledger, margin, run harness
services/research/     registry, validation toolkit, sandbox, research loop
services/risk/         pre-trade limits, kill switch, portfolio construction
services/paper-trading/ feeds and paper session
services/execution/    live gate, order router, broker adapters
services/notify/       WhatsApp notifier
packages/              instruments & rules, indicators, strategies, analytics, typed contracts
agents/                the research team and optional LLM client
tests/                 unit, integration, regression (golden), acceptance
infra/                 Dockerfiles, compose, Caddy
docs/                  architecture, status, policies, journal
datasets/ experiments/ git-ignored: private data and generated output never enter the repo
```

All Python is one import namespace, `nseql`, mapped onto those folders in `pyproject.toml`.

## Documents

[Architecture](docs/ARCHITECTURE.md) · [Implementation status](docs/IMPLEMENTATION_STATUS.md) ·
[Test report](docs/TEST_REPORT.md) · [Data provenance](docs/DATA_PROVENANCE.md) ·
[Risk policy](docs/RISK_POLICY.md) · [Live readiness checklist](docs/LIVE_READINESS_CHECKLIST.md) ·
[Research journal](docs/RESEARCH_JOURNAL.md) · [Learning Brain](docs/LEARNING_BRAIN.md) · [One-minute screening](docs/SCREENING.md) · [V5 status](docs/PLATFORM_V5_STATUS.md) · [Windows](docs/WINDOWS.md) · [Audit, Nov 2026](docs/AUDIT_2026-11.md) · [Deployment](docs/DEPLOYMENT.md) · [Contributing](CONTRIBUTING.md)
