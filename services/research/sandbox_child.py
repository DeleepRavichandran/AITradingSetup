"""Entry point of the sandbox child process. Runs jobs for one generated strategy."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(work: str) -> int:
    from nseql.backtester.runner import BacktestConfig, default_symbols, run_backtest
    from nseql.data.store import Dataset
    from nseql.research.sandbox import load_strategy_class

    w = Path(work)
    spec = json.loads((w / "jobs.json").read_text())
    cls = load_strategy_class((w / "strategy_source.txt").read_text())
    data_dir = Path(spec["data_dir"])
    out_dir = Path(spec["out_dir"]) if spec.get("out_dir") else None
    results = []
    portals: dict = {}
    for job in spec["jobs"]:
        cfg = BacktestConfig(**job["config"])
        factory = lambda ref: cls(**ref.params)
        try:
            ds = Dataset(data_dir / cfg.dataset_id)
            key = (cfg.dataset_id, cfg.interval, cfg.start, cfg.end)
            if job.get("kind", "backtest") == "lookahead_audit":
                from nseql.research.validation import lookahead_audit
                results.append({"id": job.get("id"), "kind": "lookahead_audit", "ok": True,
                                **lookahead_audit(ds, cfg, factory)})
                continue
            if key not in portals:
                portals[key] = default_symbols(ds, [factory(cfg.strategies[0])], cfg.interval)
            cfg.symbols = cfg.symbols or portals[key]
            r = run_backtest(cfg, data_dir, strategy_factory=factory, dataset=ds)
            saved = None
            if out_dir and job.get("save"):
                saved = str(r.save(out_dir / r.run_id))
            eq = r.equity_frame()
            daily = eq.groupby(eq["ts"].dt.normalize())["equity"].last()
            results.append({"id": job.get("id"), "kind": "backtest", "ok": r.summary["status"] == "ok", "run_id": r.run_id,
                            "summary": r.summary, "path": saved,
                            "daily_equity": {"dates": [str(d.date()) for d in daily.index], "values": [float(x) for x in daily]},
                            "trades": [{"net": t["net"], "exit_ts": t["exit_ts"], "symbol": t["symbol"]} for t in r.trades()]})
        except Exception as ex:  # report, do not crash the batch
            results.append({"id": job.get("id"), "kind": job.get("kind", "backtest"), "ok": False,
                            "error": f"{type(ex).__name__}: {ex}"})
    (w / "results.json").write_text(json.dumps({"ok": all(r["ok"] for r in results), "results": results, "error": None},
                                               default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
