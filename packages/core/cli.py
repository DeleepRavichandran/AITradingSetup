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
    am = sub.add_parser("import-amfi", help="import mutual-fund NAVs from AMFI files you downloaded (NAVAll.txt or NAV history reports)")
    am.add_argument("files", nargs="+")
    sub.add_parser("angel-check", help="test the Angel One login from .env (automatic TOTP) and fetch one candle")
    av = sub.add_parser("angel-verify", help="verify the Angel One adapter against the real service; writes experiments/angel_verification.json")
    av.add_argument("--symbol", help="symbol to test with (default: first of the watchlist)")
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
    wi = sub.add_parser("what-if", help="replay a paper account's stored day under the current and the new live rules, side by side")
    wi.add_argument("--account", required=True, help="folder name under experiments/screening, e.g. live_angelone_100000_r2")
    wi.add_argument("--day", required=True, help="YYYY-MM-DD"); wi.add_argument("--out", default=None, help="empty folder for the replays")
    tr = sub.add_parser("trade-report", help="root-cause report for one paper day: every round trip grouped by exit reason, strategy, stock, hour")
    tr.add_argument("--account", required=True); tr.add_argument("--day", required=True, help="YYYY-MM-DD")
    rc = sub.add_parser("trade-rca", help="write a root cause for every completed trade of a day (winners and losers) into the account")
    rc.add_argument("--account", required=True); rc.add_argument("--day", required=True, help="YYYY-MM-DD")
    la = sub.add_parser("ledger-audit", help="read-only check of a paper ledger for traces of the 2026-11-24 measurement bugs")
    la.add_argument("--account", required=True)
    co = sub.add_parser("contest", help="incumbent vs challenger in isolated paper accounts: development run, frozen manifest, holdout run, verdict")
    g = co.add_mutually_exclusive_group(required=True)
    g.add_argument("--fixture", action="store_true", help="deterministic SYNTHETIC minute data (works from a clean checkout)")
    g.add_argument("--dataset", help="a stored dataset with 1-minute bars (historical replay)")
    co.add_argument("--symbols", type=int, default=10); co.add_argument("--dev", type=int, default=5, help="development sessions")
    co.add_argument("--eval", type=int, default=15, help="holdout (evaluation) sessions"); co.add_argument("--capital", type=float, default=100_000)
    co.add_argument("--seed", type=int, default=7); co.add_argument("--out", default=None)
    rb = sub.add_parser("robustness", help="Monte Carlo trade shuffle, walk-forward and parameter sensitivity for the challenger (and Monte Carlo for the incumbent)")
    g2 = rb.add_mutually_exclusive_group(required=True)
    g2.add_argument("--fixture", action="store_true", help="deterministic SYNTHETIC minute data (works from a clean checkout)")
    g2.add_argument("--dataset", help="a stored dataset with 1-minute bars (historical replay)")
    g2.add_argument("--account", help="Monte Carlo only, on an existing paper account's closed trades (e.g. the live account)")
    rb.add_argument("--symbols", type=int, default=8); rb.add_argument("--days", type=int, default=20); rb.add_argument("--window", type=int, default=5)
    rb.add_argument("--capital", type=float, default=100_000); rb.add_argument("--seed", type=int, default=7)
    rb.add_argument("--workers", type=int, default=4); rb.add_argument("--no-incumbent", action="store_true"); rb.add_argument("--out", default=None)
    ls = sub.add_parser("loss-study", help="why a day's paper trades lost: entry context of every round trip, losers vs winners by factor")
    ls.add_argument("--account", required=True); ls.add_argument("--day", required=True, help="YYYY-MM-DD"); ls.add_argument("--out", default=None)
    db_ = sub.add_parser("day-backtest", help="every registered strategy on its own (own Rs 1,00,000) over one stored session, plus the challenger")
    db_.add_argument("--account", required=True); db_.add_argument("--day", required=True, help="YYYY-MM-DD"); db_.add_argument("--out", default=None)
    dr = sub.add_parser("daily-review", help="the automatic after-close review for one session (loss study, day backtest, Monte Carlo); "
                                             "the lab runs it by itself after 15:30")
    dr.add_argument("--account", required=True); dr.add_argument("--day", required=True, help="YYYY-MM-DD")
    dr.add_argument("--force", action="store_true", help="run again even if the review is finished")
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
    elif a.cmd == "import-amfi":
        from nseql.data.funds import FundStore
        fs = FundStore(s.data_dir)
        for f in a.files:
            out = fs.import_text(Path(f).read_text(encoding="utf-8", errors="replace"), file=Path(f).name)
            print(f"{f}: {out['layout']} layout, {out['schemes']} schemes, {out['new_navs']} new NAVs ({out['duplicates']} already stored, {out['skipped']} skipped)")
    elif a.cmd == "robustness":
        from datetime import datetime as _dt

        from nseql.screening import robustness as R
        if a.account:
            rep = R.ledger_monte_carlo(s.experiments_dir / "screening" / a.account)
            print(json.dumps(rep, indent=2))
            return 0
        out = Path(a.out) if a.out else s.experiments_dir / "robustness" / f"{'fixture' if a.fixture else a.dataset}_{_dt.now():%Y%m%d_%H%M%S}"
        if a.fixture:
            rep = R.run_fixture(out, symbols_n=a.symbols, days_n=a.days, window=a.window, seed=a.seed, capital=a.capital, workers=a.workers,
                                incumbent=not a.no_incumbent)
        else:
            from nseql.data.store import Dataset
            from nseql.screening.contest import stream_from_frames
            ds = Dataset(s.data_dir / a.dataset)
            syms = [x for x in ds.symbols("1m") if not x.startswith("NIFTY")]
            stream = stream_from_frames({x: ds.bars(x, "1m") for x in syms})
            rep = R.run(out, stream, syms, a.capital, "synthetic" if ds.synthetic else "historical", a.window, a.workers, None,
                        {"dataset": a.dataset, "synthetic": ds.synthetic})
        print(f"{rep['kind_label']}: {len(rep['sessions'])} sessions, {rep['symbols']} symbols, {rep['variants_run']} challenger variants")
        for who, m in rep["monte_carlo"].items():
            print(f"1. Monte Carlo ({who}, {m.get('trades', 0)} trades, {m.get('shuffles', 0)} shuffles): " + m.get("plain", m.get("status", "")))
        wf = rep["walk_forward"]
        print("2. Walk-forward: " + wf.get("plain", wf.get("status", "")) + f" -> {wf.get('verdict', 'n/a')}")
        for f in wf.get("folds", []):
            print(f"     in {f['in_sample'][0]}..{f['in_sample'][1]} chose {f['chosen']} {f['in_sample_return']:+.2%}; "
                  f"next {f['out_of_sample'][0]}..{f['out_of_sample'][1]} {f['out_of_sample_return']:+.2%} (frozen default {f['default_out_of_sample_return']:+.2%})")
        sv = rep["sensitivity"]
        print(f"3. Sensitivity: baseline {sv['baseline_return']:+.2%} -> {sv['verdict']} ({sv['plain']})")
        for x in sv["neighbours"]:
            print(f"     {x['parameter']}={x['value']} (default {x['default']}): {x['return']:+.2%}, {x['trades']} trades"
                  + ("  (no effect on this data)" if x.get("no_effect") else ""))
        print(f"report {out / 'report.json'}")
        return 0
    elif a.cmd == "contest":
        from datetime import datetime as _dt

        from nseql.screening import contest as C
        out = Path(a.out) if a.out else s.experiments_dir / "contest" / f"{'fixture' if a.fixture else a.dataset}_{_dt.now():%Y%m%d_%H%M%S}"
        if a.fixture:
            rep = C.run_fixture(out, symbols_n=a.symbols, dev_n=a.dev, eval_n=a.eval, seed=a.seed, capital=a.capital)
        else:
            from nseql.data.store import Dataset
            ds = Dataset(s.data_dir / a.dataset)
            syms = [x for x in ds.symbols("1m") if not x.startswith("NIFTY")]
            frames = {x: ds.bars(x, "1m") for x in syms}
            stream = C.stream_from_frames(frames)
            days = sorted({t.strftime("%Y-%m-%d") for t, _ in stream})
            if len(days) < a.dev + 2:
                print(f"dataset {a.dataset} has {len(days)} sessions of 1-minute bars; need at least {a.dev + 2}"); return 2
            ev = days[a.dev:a.dev + a.eval]
            rep = C.run_protocol(out, stream, syms, days[:a.dev], ev, a.capital, "synthetic" if ds.synthetic else "historical",
                                 {"dataset": a.dataset, "synthetic": ds.synthetic})
        i, c = rep["incumbent"], rep["challenger"]
        print(f"{rep['kind_label']}: {len(rep['sessions'])} evaluation sessions {rep['sessions'][0]} .. {rep['sessions'][-1]}")
        print(f"incumbent: {i['version']}; challenger: {c['version']}; starting capital {rep['capital']:,.0f} each")
        print(f"{'':22}{'net P&L':>12}{'max DD':>9}{'worst day':>10}{'trades':>8}{'charges':>10}{'turnover':>9}{'exposure':>9}")
        for lab, x in (("incumbent", i), ("challenger", c)):
            print(f"{lab:22}{x['net_pnl_inr']:>12,.0f}{x['max_drawdown']:>9.2%}{x['worst_day']:>10.2%}{x['trades_closed']:>8}"
                  f"{x['charges_inr']:>10,.0f}{x['turnover']:>9.2f}{x['exposure_avg']:>9.1%}")
        print(f"{'cash (reference)':22}{0:>12,.0f}{0:>9.2%}")
        p = rep["primary"]
        print(f"paired daily advantage (challenger - incumbent): mean {p['mean']:+.4%}, 95% block-bootstrap [{p['low']:+.4%}, {p['high']:+.4%}]")
        if rep["both_lost_money"]:
            print("both traders lost money; cash did better than either")
        print(f"verdict: {rep['verdict']['status']} - {rep['verdict']['reason']}")
        print(f"manifest sha256 {rep['manifest_sha256']}; report {out / 'report.json'}")
        return 0
    elif a.cmd == "ledger-audit":
        from nseql.screening.ledger_audit import audit
        rep = audit(s.experiments_dir / "screening" / a.account)
        print(json.dumps(rep, indent=2))
        return 0
    elif a.cmd == "daily-review":
        from nseql.screening.daily_review import review_dir, run as _rv
        rev = _rv(s.experiments_dir / "screening" / a.account, a.day, s.experiments_dir, force=a.force)
        for line in rev["headline"]:
            print("-", line)
        print(rev["caution"])
        print(f"saved: {review_dir(s.experiments_dir, a.account, a.day)}")
        return 0
    elif a.cmd == "day-backtest":
        from datetime import datetime as _dt

        from nseql.screening.day_backtest import run as _db
        out = Path(a.out) if a.out else s.experiments_dir / "day_backtest" / f"{a.account}_{a.day}_{_dt.now():%H%M%S}"
        print("running every strategy on its own over the stored day (several minutes)...")
        r = _db(s.experiments_dir / "screening" / a.account, a.day, out)
        print(f"{r['day']}: {r['strategies']:,} strategies on {r['symbols']} stocks, each with its own Rs 1,00,000; {r['traded']:,} traded "
              f"({r['trades']:,} round trips); {r['positive']:,} made money after charges ({(r['share_positive'] or 0):.0%}; luck baseline about 50%)")
        print(f"all strategies together: net {r['net_inr']:,.0f} INR after {r['charges_inr']:,.0f} INR charges")
        print(f"challenger: {r['challenger']['trades']} trades, net {r['challenger']['net_inr']:,.0f} INR")
        for key, label in (("by_family", "strategy type"), ("by_timeframe", "chart speed"), ("by_stop", "stop size")):
            print(f"BY {label}:")
            for g in r[key]:
                print(f"   {g['group']:<12} strategies {g['strategies']:>5}  traded {g['traded']:>5}  trades {g['trades']:>5}  net {g['net_inr']:>10,.0f}  "
                      f"per trade {g['net_per_trade'] if g['net_per_trade'] is not None else '-':>7}  positive {('%d%%' % round(100 * g['share_positive'])) if g['share_positive'] is not None else '-':>5}")
        for key, label in (("best_10", "BEST 10 (one day, partly luck)"), ("worst_10", "WORST 10")):
            print(label + ":")
            for x in r[key]:
                print(f"   {x['symbol']:<11} {x['family']:<10} {x['timeframe']:<4} stop {x['stop_pct']:.1%}  trades {x['trades']:>3}  net {x['net_inr']:>9,.2f}  {x['params']}")
        print(r["caution"])
        print(f"full report: {out / 'day_backtest.json'}")
        return 0
    elif a.cmd == "loss-study":
        from datetime import datetime as _dt

        from nseql.screening.loss_study import run as _ls
        out = Path(a.out) if a.out else s.experiments_dir / "loss_study" / f"{a.account}_{a.day}_{_dt.now():%H%M%S}"
        print("replaying the stored day with the trade journal on (a few minutes)...")
        rep = _ls(s.experiments_dir / "screening" / a.account, a.day, out)
        print(f"{rep['day']}: {rep['trades']} round trips, net {rep['net_inr']:,.0f} INR, win rate {rep['win_rate']:.0%}, "
              f"mean {rep['mean_inr']:.2f} INR per trade; replay matches the live ledger: {rep['replay_matches_live']} "
              f"(replay {rep['replay_net_inr']:,.2f}, live {rep['live_net_inr']:,.2f})")
        print("FINDINGS (lose more than the day's average, beyond chance on this day):")
        for f in rep["findings"] or [{"plain": "none: no factor separates losers from winners clearly on this day"}]:
            print("  -", f["plain"])
        for f in rep["factors"]:
            print(f"BY {f['label']}:")
            for b in f["buckets"]:
                print(f"   {b['bucket']:<34} trades {b['trades']:>4}  win {b['win_rate']:>4.0%}  net {b['net_inr']:>8,.0f}  "
                      f"mean {b['mean_inr']:>7.2f}  95% [{b['ci95'][0]:.2f}, {b['ci95'][1]:.2f}]{'  <- finding' if b['finding'] else ''}")
        for tf in rep.get("two_factor", []):
            print(f"TWO FACTORS: {tf['label_a']} x {tf['label_b']}:")
            for c in tf["cells"]:
                print(f"   {c['a']:<28} | {c['b']:<26} trades {c['trades']:>4}  win {c['win_rate']:>4.0%}  mean {c['mean_inr']:>7.2f}  "
                      f"[{c['ci95'][0]:.2f}, {c['ci95'][1]:.2f}]{'  <- finding' if c['finding'] else ''}")
        print(rep["caution"])
        print(f"full report: {out / 'loss_study.json'}")
        return 0
    elif a.cmd == "trade-rca":
        import sqlite3 as _sq
        from nseql.screening.rca import analyse, summary
        db = _sq.connect(str(s.experiments_dir / "screening" / a.account / "screening.sqlite"), isolation_level=None)
        try:
            rc = analyse(db, a.day)
        finally:
            db.close()
        sm = summary(rc)
        print(f"{len(rc)} trades documented: {sm['won']} won, {sm['lost']} lost")
        for g in sm["by_cause"]:
            print(f"  {g['outcome']:<5} {g['trades']:>4} trades  net {g['net_inr']:>9,.0f}  {g['cause']}")
        print("examples:")
        for t in sorted(rc, key=lambda t: t["net_inr"])[:5] + sorted(rc, key=lambda t: -t["net_inr"])[:3]:
            print(f"  {str(t['exit_ts'])[11:16]} {t['symbol']:<11} {t['summary']}" + (f" [{'; '.join(t['causes'])}]" if t["causes"] else ""))
        return 0
    elif a.cmd == "trade-report":
        from nseql.screening.trade_report import report as _tr
        rep = _tr(s.experiments_dir / "screening" / a.account, a.day)
        out = s.experiments_dir / "screening" / a.account / f"trade_report_{a.day}.json"
        out.write_text(json.dumps(rep, indent=2, default=str))
        print("TOTALS", json.dumps(rep["totals"]))
        print("FINDINGS"); [print(" -", x) for x in rep["findings"]]
        for k in ("exit_reason", "timeframe", "family", "hold", "entry_hour"):
            print(f"BY {k}:"); [print(f"   {g['group']:<26} trades {int(g['trades']):>4}  net {g['net_inr']:>9,.0f}  gross {g['gross_inr']:>9,.0f}  "
                                       f"win {g['win_rate']:.0%}  hold {g['avg_hold_min']:.0f}m") for g in rep["by"][k]]
        print("ROOT CAUSES (every trade):"); [print(f"   {g['outcome']:<5} {g['trades']:>4} trades  net {g['net_inr']:>9,.0f}  {g['cause']}") for g in rep["rca"]["by_cause"]]
        print("WORST 5 STOCKS:"); [print(f"   {g['group']:<12} trades {int(g['trades']):>4}  net {g['net_inr']:>8,.0f}") for g in rep["by"]["symbol"][:5]]
        print(f"full report: {out}")
        return 0
    elif a.cmd == "what-if":
        import json as _json
        from datetime import datetime as _dt

        from nseql.screening.whatif import what_if
        src = s.experiments_dir / "screening" / a.account
        out = Path(a.out) if a.out else s.experiments_dir / "screening" / "what_if" / f"{a.account}_{a.day}_{_dt.now():%H%M%S}"
        rep = what_if(src, a.day, out)
        (out / "report.json").write_text(_json.dumps(rep, indent=2))
        print(_json.dumps(rep, indent=2))
        return 0
    elif a.cmd == "angel-verify":
        from nseql.data.adapters.angel_verify import verify
        rep = verify(s.experiments_dir, symbol=a.symbol)
        return 0 if rep["passed"] else 1
    elif a.cmd in ("angel-check", "import-angelone"):
        from datetime import date as _d
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        from nseql.data.adapters.angelone import AngelError, AngelHistorical, missing_credentials, resolve
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
        from nseql.evaluation import cache as cache_mod
        from nseql.evaluation import context_test
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
