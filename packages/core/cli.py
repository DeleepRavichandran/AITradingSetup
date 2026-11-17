"""Command line: nseql <command>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nseql.config import get_settings


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(prog="nseql", description="NSE AI Quant Lab (research and paper trading)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fixtures", help="generate the deterministic SYNTHETIC fixture dataset")
    b = sub.add_parser("backtest", help="run one backtest and print the summary")
    b.add_argument("strategy"); b.add_argument("--params", default="{}"); b.add_argument("--dataset", default="synthetic_v1")
    b.add_argument("--start"); b.add_argument("--end"); b.add_argument("--interval", default="1d")
    b.add_argument("--capital", type=float, default=1_000_000); b.add_argument("--trades", action="store_true", help="print every trade")
    r = sub.add_parser("research", help="run the autonomous research loop")
    r.add_argument("--iterations", type=int, default=3); r.add_argument("--max-backtests", type=int, default=300)
    r.add_argument("--dataset", default="synthetic_v1"); r.add_argument("--seed", type=int, default=0)
    q = sub.add_parser("quality", help="data-quality report for a dataset"); q.add_argument("dataset"); q.add_argument("--interval", default="1d")
    i = sub.add_parser("import-bhavcopy", help="import NSE bhavcopy files you are licensed to use")
    i.add_argument("dataset_id"); i.add_argument("files", nargs="+"); i.add_argument("--licence", required=True)
    kl = sub.add_parser("kite-login", help="daily Kite login: prints the login URL, then exchanges the request_token for an access token")
    kl.add_argument("--request-token", help="the request_token from the redirect URL after you log in")
    ik = sub.add_parser("import-kite", help="import equity/index candles from Kite Connect into a dataset")
    ik.add_argument("dataset_id"); ik.add_argument("--symbols", help="comma separated, e.g. RELIANCE,TCS,INFY")
    ik.add_argument("--symbols-file", help="one symbol per line"); ik.add_argument("--start", required=True); ik.add_argument("--end")
    ik.add_argument("--interval", default="1d"); ik.add_argument("--licence", required=True)
    v = sub.add_parser("serve", help="start the API server"); v.add_argument("--host", default="127.0.0.1"); v.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    if a.cmd == "fixtures":
        from nseql.data.fixtures import generate_fixture
        d = generate_fixture(s.data_dir, overwrite=True)
        print(f"wrote {d.root} (SYNTHETIC) content_hash={d.content_hash}")
    elif a.cmd == "backtest":
        from nseql.backtester.runner import BacktestConfig, StrategyRef, run_backtest
        from nseql.research.registry import Registry
        res = run_backtest(BacktestConfig(dataset_id=a.dataset, interval=a.interval, start=a.start, end=a.end,
                                          strategies=[StrategyRef(name=a.strategy, params=json.loads(a.params), capital=a.capital)]), s.data_dir)
        path = res.save(s.experiments_dir / "runs" / res.run_id)
        Registry(s.registry_path).record_run(res.run_id, res.summary, path=str(path))
        m = res.metrics
        print(f"run {res.run_id}  status={res.summary['status']}  dataset={a.dataset}" + ("  [SYNTHETIC DATA]" if res.summary["dataset"]["synthetic"] else ""))
        for k in ("total_return", "cagr", "sharpe", "sortino", "calmar", "max_drawdown", "max_drawdown_days", "trades", "win_rate",
                  "profit_factor", "expectancy", "avg_holding_days", "total_costs", "turnover_annual", "exposure_time", "capacity_estimate"):
            print(f"  {k:22s} {m.get(k)}")
        print(f"  reconciliation         {res.summary['reconciliation']}")
        print(f"  result_hash            {res.summary['result_hash']}")
        if a.trades:
            for t in res.trades():
                print(f"  {t['entry_ts'][:10]} -> {t['exit_ts'][:10]}  {t['symbol']:<22} qty {t['qty']:>6}  {t['entry_price']:>10.2f} -> {t['exit_price']:>10.2f}"
                      f"  net {t['net']:>12,.2f}  ({t['exit_reason']})")
        print(f"artefacts: {path}")
    elif a.cmd == "research":
        from nseql.agents.llm import default_client
        from nseql.research.loop import ResearchBudget, ResearchLoop
        from nseql.research.registry import Registry
        out = ResearchLoop(s.data_dir, s.experiments_dir, Registry(s.registry_path), dataset_id=a.dataset, seed=a.seed, llm=default_client(),
                           budget=ResearchBudget(max_iterations=a.iterations, max_backtests=a.max_backtests)).run()
        print(f"experiment {out['experiment_id']}  status={out['status']}  usage={out['usage']}  inventor={out['inventor']}")
        for x in out["results"]:
            print(f"\n=== {x['hypothesis_id']} [{x['family']}] -> {x['decision']} {x['reasons']}\n{x['explanation']}")
    elif a.cmd == "quality":
        from nseql.data.quality import dataset_quality
        from nseql.data.store import Dataset
        rep = dataset_quality(Dataset(s.data_dir / a.dataset), a.interval)
        print(json.dumps({k: v for k, v in rep.items() if k not in ("coverage", "issues")}, indent=1))
    elif a.cmd == "import-bhavcopy":
        from nseql.data.adapters.bhavcopy import import_bhavcopies
        d = import_bhavcopies([Path(f) for f in a.files], s.data_dir, a.dataset_id, licence=a.licence)
        print(f"imported {len(d.instruments)} instruments into {d.root}; content_hash={d.content_hash}")
    elif a.cmd == "kite-login":
        import os

        from nseql.data.adapters.kite import exchange_request_token, login_url, save_session
        key, secret = os.environ.get("KITE_API_KEY", ""), os.environ.get("KITE_API_SECRET", "")
        if not key or not secret:
            print("set KITE_API_KEY and KITE_API_SECRET in the environment first"); return 2
        if not a.request_token:
            print("1. Open this URL and log in:\n   " + login_url(key) + "\n2. After login you are redirected to your registered redirect URL;\n"
                  "   copy the request_token=... value from that address, then run:\n   nseql kite-login --request-token <value>")
            return 0
        data = exchange_request_token(key, secret, a.request_token)
        save_session(s.experiments_dir, key, data["access_token"])
        print(f"logged in as {data.get('user_id')}; token saved to {s.experiments_dir}/kite_session.json (valid until about 06:00 IST tomorrow)")
    elif a.cmd == "import-kite":
        import os
        from datetime import date as _d

        from nseql.data.adapters.kite import KiteHistorical, load_session
        from nseql.data.adapters.kite_import import import_kite
        ses = load_session(s.experiments_dir) or {}
        api = KiteHistorical(os.environ.get("KITE_API_KEY") or ses.get("api_key"), os.environ.get("KITE_ACCESS_TOKEN") or ses.get("access_token"))
        syms = [x.strip() for x in (a.symbols or "").split(",") if x.strip()]
        if a.symbols_file:
            syms += [l.strip() for l in Path(a.symbols_file).read_text().splitlines() if l.strip() and not l.startswith("#")]
        if not syms:
            print("give --symbols or --symbols-file (I do not hard-code index constituents: they change)"); return 2
        ds, missing = import_kite(api, syms, _d.fromisoformat(a.start), _d.fromisoformat(a.end) if a.end else _d.today(), s.data_dir, a.dataset_id,
                                  licence=a.licence, interval=a.interval)
        print(f"dataset {ds.id}: {len(ds.instruments)} instruments, content_hash={ds.content_hash}")
        if missing:
            print("not imported:", ", ".join(missing))
        print("next: nseql quality", ds.id)
    elif a.cmd == "serve":
        import os

        import uvicorn
        if not os.environ.get("NSEQL_API_TOKEN") and os.environ.get("NSEQL_ALLOW_INSECURE_LOCAL", "").lower() != "true":
            print("NSEQL_API_TOKEN is not set. Generate one with:\n  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
                  "and export it, or — on a single-user machine only — set NSEQL_ALLOW_INSECURE_LOCAL=true (loopback clients only).")
            return 2
        uvicorn.run("nseql.api.main:app_factory", factory=True, host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
