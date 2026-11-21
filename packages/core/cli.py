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
    tv = sub.add_parser("import-tradingview", help="import CSV files exported from TradingView charts (manual export)")
    tv.add_argument("dataset_id"); tv.add_argument("files", nargs="+", help="e.g. 'NSE_RELIANCE, 1D.csv'"); tv.add_argument("--licence", required=True)
    tv.add_argument("--interval", help="override the interval read from the file names (1d, 5m, ...)")
    sub.add_parser("angel-check", help="test the Angel One login from .env (automatic TOTP) and fetch one candle")
    ia = sub.add_parser("import-angelone", help="import candles from Angel One SmartAPI (free for Angel One clients)")
    ia.add_argument("dataset_id"); ia.add_argument("--symbols", help="comma separated; default: the watchlist")
    ia.add_argument("--start", required=True); ia.add_argument("--end"); ia.add_argument("--interval", default="1d")
    ia.add_argument("--licence", default="Angel One SmartAPI, personal research")
    kl = sub.add_parser("kite-login", help="daily Kite login: prints the login URL, then exchanges the request_token for an access token")
    kl.add_argument("--request-token", help="the request_token from the redirect URL after you log in")
    ik = sub.add_parser("import-kite", help="import equity/index candles from Kite Connect into a dataset")
    ik.add_argument("dataset_id"); ik.add_argument("--symbols", help="comma separated, e.g. RELIANCE,TCS,INFY")
    ik.add_argument("--symbols-file", help="one symbol per line"); ik.add_argument("--start", required=True); ik.add_argument("--end")
    ik.add_argument("--interval", default="1d"); ik.add_argument("--licence", required=True)
    bb = sub.add_parser("brain-benchmark", help="learning benchmark: memory-informed vs memory-free research on unseen windows")
    bb.add_argument("--dataset", default="synthetic_v1"); bb.add_argument("--negative-control", action="store_true")
    bb.add_argument("--final-sealed", action="store_true", help="ONE-SHOT: score on the sealed window (recorded; refused the second time)")
    ca = sub.add_parser("corporate-actions", help="find likely splits/bonuses in a dataset, or attach confirmed ones from a CSV")
    ca.add_argument("dataset"); ca.add_argument("--file", help="CSV: symbol,ex_date,kind,ratio,amount")
    ca.add_argument("--new-id", help="id of the new dataset version (datasets are immutable)")
    ca.add_argument("--source", help="where the actions were confirmed, e.g. NSE corporate announcements")
    ca.add_argument("--force", action="store_true", help="attach even if the prices do not show the drop (rarely right)")
    ic = sub.add_parser("import-context", help="import market context from Kite: India VIX, sector indices, USDINR, MCX crude and gold")
    ic.add_argument("dataset_id", nargs="?", default="context_v1"); ic.add_argument("--start", required=True); ic.add_argument("--end")
    ic.add_argument("--licence", required=True); ic.add_argument("--only", help="comma separated subset, e.g. INDIAVIX,NIFTYBANK")
    ct = sub.add_parser("context-test", help="test whether each market-context filter beats a placebo (development+validation data only)")
    ct.add_argument("--dataset", required=True); ct.add_argument("--context", default="context_v1",
                    help="context dataset; scheduled events from `nseql events` are included automatically")
    ct.add_argument("--pairs", type=int, default=150); ct.add_argument("--placebos", type=int, default=20)
    ev = sub.add_parser("events", help="load scheduled events (results dates, RBI, Budget, FOMC) or generate F&O expiries")
    ev.add_argument("--file", help="CSV: event_date,kind,known_from,label,symbol")
    ev.add_argument("--source", help="where the dates came from"); ev.add_argument("--licence", default="operator-supplied")
    ev.add_argument("--expiries", nargs=2, metavar=("START", "END"), help="generate monthly F&O expiry dates for this range")
    hd = sub.add_parser("holidays", help="load the NSE trading-holiday list (CSV copied from the exchange circular) for paper trading")
    hd.add_argument("file", nargs="?", help="CSV with a Date column (e.g. 26-Jan-2026) and optionally a Description column")
    hd.add_argument("--source", help="where the list came from, e.g. the NSE circular number and date")
    hd.add_argument("--show", action="store_true", help="print the loaded list")
    sub.add_parser("brain-verify", help="verify the episodic memory hash chain and the correctness probes")
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
        from nseql.research.loop import ResearchBudget, ResearchLoop, SandboxUnavailable
        from nseql.research.registry import Registry
        out = ResearchLoop(s.data_dir, s.experiments_dir, Registry(s.registry_path), dataset_id=a.dataset, seed=a.seed, llm=default_client(),
                           budget=ResearchBudget(max_iterations=a.iterations, max_backtests=a.max_backtests))
        try:
            out = out.run()
        except SandboxUnavailable as e:
            print(e); print("Backtests, screening and paper trading still work; research needs Linux or Docker Desktop."); return 2
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
    elif a.cmd == "import-tradingview":
        import glob

        from nseql.data.adapters.tradingview import import_tradingview
        files = [Path(x) for f in a.files for x in (sorted(glob.glob(f)) or [f])]
        ds, notes = import_tradingview(files, s.data_dir, a.dataset_id, licence=a.licence, interval=a.interval)
        print(f"dataset {ds.id}: {len(ds.instruments)} symbols, content_hash={ds.content_hash}")
        for n in notes:
            print("note:", n)
        from nseql.data.corporate import scan_dataset
        if ds.intervals() == ["1d"] and scan_dataset(ds):
            print(f"WARNING: price drops that look like splits/bonuses; see `nseql corporate-actions {ds.id}`")
        print("next: nseql quality", ds.id)
    elif a.cmd in ("angel-check", "import-angelone"):
        from datetime import date as _d
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        from nseql.data.adapters.angelone import AngelError, AngelHistorical, missing_credentials, resolve  # noqa: I001
        miss = missing_credentials()
        if miss:
            print("missing in .env:", ", ".join(miss)); return 2
        from nseql.data.adapters.angelone import watchlist
        watch = watchlist(s.experiments_dir)
        try:
            api = AngelHistorical(session_dir=s.experiments_dir)
            if a.cmd == "angel-check":
                api.login()
                print(f"login OK for client {api.client_code} (session saved; renewed automatically each day)")
                found, missing = resolve(api.instruments(s.experiments_dir), [*watch, "NIFTY 50"])
                print(f"watchlist: {len(found)} found" + (f", not found: {', '.join(missing)}" if missing else ""))
                df = api.candles(found[0]["token"], "1d", _dt.now() - _td(days=10), _dt.now())
                print(f"{found[0]['symbol']}: last daily candle {df['ts'].iloc[-1]:%Y-%m-%d} close {df['close'].iloc[-1]}" if len(df) else "no candles returned")
                return 0
            from nseql.data.adapters.angel_import import import_angel
            syms = [x.strip() for x in a.symbols.split(",")] if a.symbols else watch
            ds, missing = import_angel(api, syms, _d.fromisoformat(a.start), _d.fromisoformat(a.end) if a.end else _d.today(), s.data_dir,
                                       a.dataset_id, licence=a.licence, interval=a.interval, cache_dir=s.experiments_dir)
        except AngelError as e:
            print(e); return 1
        print(f"dataset {ds.id}: {len(ds.instruments)} instruments, content_hash={ds.content_hash}")
        if missing:
            print("not imported:", ", ".join(missing))
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
        from nseql.data.corporate import scan_dataset
        sus = scan_dataset(ds)
        if sus:
            print(f"WARNING: {len(sus)} price drop(s) look like unlisted splits/bonuses; see `nseql corporate-actions {ds.id}`")
        print("next: nseql quality", ds.id)
    elif a.cmd == "brain-benchmark":
        from nseql.brain.service import BrainService
        from nseql.data.store import Dataset
        from nseql.research.loop import get_partition
        from nseql.research.registry import Registry
        reg = Registry(s.registry_path)
        ds = Dataset(s.data_dir / a.dataset)
        part = get_partition(reg, ds)
        svc = BrainService(s.experiments_dir, s.data_dir)
        prog = lambda i, n: print(f"  decision {i}/{n}", flush=True) if i % 10 == 0 or i == n else None
        if a.final_sealed:
            reg.claim_sealed(f"brain_benchmark_v{__import__('nseql.brain.policy', fromlist=['x']).PROTOCOL_VERSION}:{ds.content_hash[:12]}", "research_director")
            svc.memory.append("system", "sealed_access", {"dataset": ds.id, "content_hash": ds.content_hash, "sealed_from": part.sealed_start},
                              available_at=part.sealed_start)
            rep = svc.run_benchmark(ds.id, end_date=None, sealed_from=part.sealed_start, label=f"{ds.id}: final sealed confirmation", progress=prog)
        else:
            rep = svc.run_benchmark(ds.id, end_date=part.val_end, label=f"{ds.id}: development+validation", progress=prog)
        print(f"report {rep['run_id']}: {rep['verdict']} (basis: {rep['verdict_basis']})")
        if a.negative_control:
            from nseql.data.fixtures import generate_null_fixture
            if not (s.data_dir / "null_v1").exists():
                generate_null_fixture(s.data_dir)
            nrep = svc.run_benchmark("null_v1", end_date=part.val_end, label="negative control (random walk)", progress=prog)
            print(f"negative control {nrep['run_id']}: {nrep['verdict']}")
    elif a.cmd == "corporate-actions":
        from nseql.data.corporate import attach_actions, read_actions_csv, scan_dataset
        from nseql.data.store import Dataset
        ds = Dataset(s.data_dir / a.dataset)
        if a.file:
            if not a.new_id or not a.source:
                print("give --new-id (a new dataset version) and --source"); return 2
            try:
                new = attach_actions(ds, read_actions_csv(a.file), s.data_dir, a.new_id, source=a.source, force=a.force)
            except ValueError as e:
                print(e); return 1
            print(f"dataset {new.id}: {len(new.corporate_actions())} corporate action(s), content_hash={new.content_hash}")
            ds = new
        found = scan_dataset(ds)
        if not found:
            print(f"{ds.id}: no unexplained split/bonus-shaped drops")
        else:
            print(f"{ds.id}: {len(found)} drop(s) that look like a split or bonus and are not listed. Check each against the")
            print("exchange's corporate announcements, then attach the real ones with --file. Do NOT add any the data is already adjusted for.")
            for x in found:
                print(f"  {x['symbol']:<14} {x['ex_date']}  ratio ~{x['ratio']:g} (open/prev close x{x['observed_drop']:g}, "
                      f"volume x{x['volume_ratio']})  {x['evidence']}")
    elif a.cmd == "import-context":
        import os
        from datetime import date as _d

        from nseql.data.adapters.kite import KiteHistorical, load_session
        from nseql.data.context import import_context
        ses = load_session(s.experiments_dir) or {}
        api = KiteHistorical(os.environ.get("KITE_API_KEY") or ses.get("api_key"), os.environ.get("KITE_ACCESS_TOKEN") or ses.get("access_token"))
        names = [x.strip().upper() for x in a.only.split(",")] if a.only else None
        ds, missing = import_context(api, _d.fromisoformat(a.start), _d.fromisoformat(a.end) if a.end else _d.today(), s.data_dir,
                                     a.dataset_id, licence=a.licence, names=names)
        print(f"context dataset {ds.id}: {', '.join(ds.symbols('1d'))}; content_hash={ds.content_hash}")
        if missing:
            print("not available on this account / not found:", ", ".join(missing),
                  "(currency and MCX series need those segments enabled on your Kite account)")
    elif a.cmd == "context-test":
        import time as _t

        from nseql.brain.service import BrainService
        from nseql.data.store import Dataset
        from nseql.evaluation import cache as cache_mod, context_test
        from nseql.evaluation.vector import side_cost
        from nseql.research.loop import get_partition
        from nseql.research.registry import Registry
        ds, cds = Dataset(s.data_dir / a.dataset), Dataset(s.data_dir / a.context)
        part = get_partition(Registry(s.registry_path), ds)
        from nseql.data.events import EventStore
        evp = s.experiments_dir / "events.sqlite"
        P = cache_mod.load(cache_mod.build(ds, s.experiments_dir / "brain" / "cache", context=cds,
                                           events=EventStore(evp) if evp.exists() else None))
        dates = [str(x) for x in P.dates()]
        lo = next(i for i, x in enumerate(dates) if x >= part.dev_start) + 130
        hi = max(i for i, x in enumerate(dates) if x <= part.val_end) + 1          # the sealed segment is never scored here
        cost = side_cost(P.dates()[(lo + hi) // 2])
        rep = context_test.run(P, cost, rows=(lo, hi), pairs=a.pairs, placebos=a.placebos,
                               progress=lambda i, n: print(f"  state {i}/{n}", flush=True))
        rep.update(dataset=ds.id, dataset_content_hash=ds.content_hash, window=[dates[lo], dates[hi - 1]], created_at=_t.strftime("%Y-%m-%dT%H:%M:%S"))
        out = s.experiments_dir / "context" / f"context_test_{_t.strftime('%Y%m%d_%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1))
        BrainService(s.experiments_dir, s.data_dir).memory.append(
            "context", "context_test", {k: rep[k] for k in ("states", "window", "dataset", "synthetic", "method")}, available_at=dates[hi - 1])
        tag = "  [SYNTHETIC — says nothing about real markets]" if rep["synthetic"] else ""
        print(f"window {dates[lo]} → {dates[hi - 1]} (sealed segment untouched){tag}")
        for st, v in rep["states"].items():
            print(f"  {st:<18} Sharpe change {v['median_sharpe_change']:+.2f} vs placebo {v['placebo_median_sharpe_change']:+.2f} "
                  f"(z {v['z_vs_placebo']:+.1f}, Holm p {v['p_holm']:.3f})  -> {v['verdict']}")
        print(f"report: {out}")
    elif a.cmd == "events":
        from datetime import date as _d

        from nseql.data import scheduled as sch
        from nseql.data.events import EventStore
        from nseql.instruments.calendar import TradingCalendar
        store = EventStore(s.experiments_dir / "events.sqlite")
        if a.file:
            if not a.source:
                print("give --source (e.g. 'NSE board-meeting intimations; RBI MPC calendar 2026-27')"); return 2
            print(f"loaded {sch.ingest_schedule_csv(store, a.file, source=a.source, licence=a.licence)} event(s)")
        if a.expiries:
            hol = s.experiments_dir / "holidays.json"
            cal = TradingCalendar.from_file(hol) if hol.exists() else TradingCalendar()
            n = sch.add_expiries(store, _d.fromisoformat(a.expiries[0]), _d.fromisoformat(a.expiries[1]), cal.is_session)
            print(f"generated {n} monthly expiry date(s)" + ("" if hol.exists() else " (no holiday list: holidays not skipped)"))
        rows = sch.scheduled(store)
        by = {}
        for r in rows:
            by[r["kind"]] = by.get(r["kind"], 0) + 1
        print(f"{len(rows)} scheduled event(s) stored: {by or 'none'}")
    elif a.cmd == "holidays":
        from nseql.instruments.calendar import TradingCalendar, holidays_from_csv
        path = s.experiments_dir / "holidays.json"
        cur = TradingCalendar.from_file(path) if path.exists() else TradingCalendar(source="")
        if a.file:
            if not a.source:
                print("give --source (the NSE circular the list came from)"); return 2
            new_cal = holidays_from_csv(a.file, source=a.source)
            cur = cur.merged(new_cal) if cur.years else new_cal
            s.experiments_dir.mkdir(parents=True, exist_ok=True)
            cur.to_file(path)
            print(f"loaded {len(new_cal.holidays)} weekday holidays for {sorted(new_cal.years)} from {a.file}; saved to {path}")
            print("restart the API (or the container) so running paper sessions pick it up")
        if a.show or not a.file:
            if not cur.holidays:
                print("no holiday list loaded: paper trading treats every weekday as a trading day"); return 0
            print(f"source: {cur.source}\nyears: {sorted(cur.years)}")
            for d in sorted(cur.holidays):
                print(f"  {d}  {d:%a}  {cur.names.get(d, '')}")
    elif a.cmd == "brain-verify":
        from nseql.brain.service import BrainService
        print(json.dumps(BrainService(s.experiments_dir, s.data_dir).memory.verify(), indent=1))
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
