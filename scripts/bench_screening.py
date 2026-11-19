"""Benchmark the one-minute screening loop at several registry sizes.

    python scripts/bench_screening.py                       # 1k, 5k, 10k, 50k; 300 symbols; 3 sessions
    python scripts/bench_screening.py --sizes 1000 --days 1 --no-recovery --no-realtime

Each size runs in a fresh child process (so peak memory is that run's own). Per minute the child pushes
that minute's bars into the event log (as a feed would) and runs one scheduler cycle. Measured:
  * cycle time (ingest + evaluation + ledger write) and engine-only time, p50/p95/p99/max
  * evaluations, decisions, minutes processed vs minutes with data, cycles over the 60 s budget
  * peak resident memory, CPU time, database and checkpoint size
Recovery: the run is killed with SIGKILL part-way, restarted, and the final ledger compared with an
uninterrupted run (must be identical). Real-time: the 50k registry runs on the wall clock for a few
minutes with the background thread, and the lag from each minute's close to its decisions is recorded.

Data: SYNTHETIC random-walk minute bars (nseql.screening.synthetic), not market data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np

DAYS = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def _ledger_hash(db_path: Path) -> str:
    import sqlite3
    db = sqlite3.connect(str(db_path))
    h = hashlib.sha256()
    for q in ("SELECT config_id, bar_ts, decision, price, reason FROM signals ORDER BY 1, 2",
              "SELECT order_key, status, fill_ts, qty, price, charges_paise, realized_paise FROM paper_orders ORDER BY 1",
              "SELECT minute, status, eligible, evaluated FROM cycles ORDER BY 1"):
        for r in db.execute(q):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()[:16]


def child(args) -> None:
    import pandas as pd

    from nseql.screening.scheduler import MinuteScheduler
    from nseql.screening.synthetic import minute_stream, symbols
    root = Path(args.root)
    t0 = time.perf_counter()
    stream = minute_stream(args.symbols, DAYS[: args.days], seed=1)
    gen_s = time.perf_counter() - t0
    sch = MinuteScheduler(root, symbols(args.symbols), args.n, checkpoint_every=args.checkpoint_every, mode="benchmark")
    start_s = sch.startup_seconds
    rec = dict(sch.recovery)
    cyc, eng, wm = [], [], sch.watermark
    t_run = time.perf_counter()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    caught_up_at = None
    for k, (ts, bars) in enumerate(stream):
        c0 = time.perf_counter()
        sch.ingest(bars, source="synthetic")
        out = sch.run_cycle(ts + pd.Timedelta(seconds=6))
        dt = (time.perf_counter() - c0) * 1000
        if out["processed"]:
            cyc.append(dt / out["processed"] if out["processed"] > 1 else dt)
            eng.extend(out["latency_ms"])
        if wm and caught_up_at is None and str(ts) >= wm.replace("T", " "):
            caught_up_at = time.perf_counter() - t_run
        if args.kill_after and k + 1 == args.kill_after:
            print(json.dumps({"killing_at_minute": str(ts)}), flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
    wall = time.perf_counter() - t_run
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    db = sch.store.db
    q = lambda s: db.execute(s).fetchone()
    with_data = len({str(ts) for ts, b in stream if b})
    processed = q("SELECT COUNT(*) FROM cycles WHERE status='processed'")[0]
    ev, el, sk = q("SELECT SUM(evaluated), SUM(eligible), SUM(skipped_pending) FROM cycles")
    counts = {}
    for (c,) in db.execute("SELECT counts FROM cycles WHERE status='processed'"):
        for kk, vv in json.loads(c).items():
            counts[kk] = counts.get(kk, 0) + vv
    pc = lambda a, p: round(float(np.percentile(a, p)), 2) if a else None
    print(json.dumps({
        "configs": len(sch.reg), "symbols": args.symbols, "sessions": args.days, "minutes_with_data": with_data,
        "minutes_processed": processed, "no_data_minutes": q("SELECT COUNT(*) FROM cycles WHERE status='no_data'")[0],
        "minutes_processed_this_run": len(eng),
        "cycle_ms": {"p50": pc(cyc, 50), "p95": pc(cyc, 95), "p99": pc(cyc, 99), "max": round(max(cyc), 2) if cyc else None},
        "engine_ms": {"p50": pc(eng, 50), "p95": pc(eng, 95), "p99": pc(eng, 99), "max": round(max(eng), 2) if eng else None},
        "cycles_over_60s_budget": int(sum(1 for x in cyc if x > 60_000)),
        "evaluations": int(ev or 0), "eligible": int(el or 0), "eligible_not_evaluated": int((el or 0) - (ev or 0)),
        "skipped_pending_order": int(sk or 0), "decisions": counts,
        "evaluations_per_second": round((ev or 0) / max(sum(eng) / 1000, 1e-9)) if eng else None,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "cpu_seconds": round((cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime), 2), "wall_seconds": round(wall, 2),
        "startup_seconds": start_s, "stream_generation_seconds": round(gen_s, 2), "recovery": rec,
        "replay_to_watermark_seconds": round(caught_up_at, 2) if caught_up_at is not None else None,
        "db_mb": round(os.path.getsize(root / "screening.sqlite") / 2**20, 1),
        "checkpoint_mb": round(os.path.getsize(sch.ckpt) / 2**20, 1) if sch.ckpt.exists() else None,
        "signals": q("SELECT COUNT(*) FROM signals")[0], "paper_orders": q("SELECT COUNT(*) FROM paper_orders")[0],
        "ledger_hash": _ledger_hash(root / "screening.sqlite"),
    }), flush=True)


def run_child(n, root, a, kill_after=0) -> tuple[dict | None, float]:
    cmd = [sys.executable, __file__, "--child", "--n", str(n), "--root", str(root), "--symbols", str(a.symbols), "--days", str(a.days),
           "--checkpoint-every", str(a.checkpoint_every), "--kill-after", str(kill_after)]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
    if kill_after:
        assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr[-2000:])
        return json.loads(lines[0]) if lines else None, dt
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-4000:])
    return json.loads(lines[-1]), dt


def realtime(n, root, a, minutes: int) -> dict:
    """The 50k registry on the wall clock: a fake clock that starts just before 09:16 of a session day, the
    replay feed delivering each minute's bars at that minute's close, and the background thread."""
    import pandas as pd

    from nseql.screening.scheduler import MinuteScheduler, ReplayMinuteFeed
    from nseql.screening.synthetic import minute_stream, symbols
    stream = minute_stream(a.symbols, DAYS[:1], seed=1)
    frames: dict = {}
    for ts, bars in stream[: minutes + 2]:
        for b in bars:
            frames.setdefault(b["symbol"], []).append(b)
    frames = {s: pd.DataFrame(v) for s, v in frames.items()}
    start = stream[0][0] - pd.Timedelta(seconds=55)
    offset = start.to_pydatetime() - datetime.now()
    sch = MinuteScheduler(root, symbols(a.symbols), n, feed=ReplayMinuteFeed(frames, "synthetic replay (wall clock)"), mode="realtime",
                          grace_seconds=2.0, clock=lambda: datetime.now() + offset)
    sch.start()
    t_end = time.time() + 60 * minutes + 10
    while time.time() < t_end:
        time.sleep(1)
    sch.stop()
    rows = sch.store.db.execute("SELECT minute, lag_s, latency_ms FROM cycles WHERE status='processed' ORDER BY minute").fetchall()
    lags = [r[1] for r in rows]
    return {"configs": len(sch.reg), "wall_minutes": minutes, "cycles": len(rows), "overruns": sch.overruns,
            "lag_s_after_minute_close": {"min": round(min(lags), 2), "max": round(max(lags), 2), "mean": round(float(np.mean(lags)), 2)} if lags else None,
            "latency_ms": [round(r[2], 1) for r in rows], "grace_seconds": 2.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", action="store_true"); ap.add_argument("--n", type=int); ap.add_argument("--root")
    ap.add_argument("--sizes", default="1000,5000,10000,50000"); ap.add_argument("--symbols", type=int, default=300)
    ap.add_argument("--days", type=int, default=3); ap.add_argument("--checkpoint-every", type=int, default=15)
    ap.add_argument("--kill-after", type=int, default=0); ap.add_argument("--no-recovery", action="store_true")
    ap.add_argument("--no-realtime", action="store_true"); ap.add_argument("--realtime-minutes", type=int, default=3)
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.child:
        return child(a)
    import numpy
    import pandas
    hw = {"cpu": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), platform.processor()),
          "logical_cpus": os.cpu_count(), "memory_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
          "python": platform.python_version(), "numpy": numpy.__version__, "pandas": pandas.__version__, "os": platform.platform()}
    rep = {"generated_at": datetime.now().isoformat(timespec="seconds"), "hardware": hw, "data": "SYNTHETIC random-walk 1-minute bars",
           "symbols": a.symbols, "sessions": a.days, "runs": {}}
    tmp = Path(tempfile.mkdtemp(prefix="nseql_bench_"))
    for n in [int(x) for x in a.sizes.split(",")]:
        res, dt = run_child(n, tmp / f"n{n}", a)
        res["process_wall_seconds"] = round(dt, 2)
        rep["runs"][str(n)] = res
        print(f"{n:>6}: cycle p50/p95/p99 {res['cycle_ms']['p50']}/{res['cycle_ms']['p95']}/{res['cycle_ms']['p99']} ms, "
              f"engine p99 {res['engine_ms']['p99']} ms, rss {res['peak_rss_mb']} MB, missed {res['eligible_not_evaluated']}", flush=True)
        if not a.no_recovery:
            total = a.days * 375
            kill = int(total * 0.6) + 7
            d = tmp / f"n{n}_crash"
            killed, _ = run_child(n, d, a, kill_after=kill)
            resumed, dt2 = run_child(n, d, a)
            rep["runs"][str(n)]["recovery_test"] = {
                "killed_after_minutes": kill, "killed_at": (killed or {}).get("killing_at_minute"),
                "restart_startup_seconds": resumed["startup_seconds"], "checkpoint_load_seconds": resumed["recovery"]["seconds"],
                "checkpoint_watermark": resumed["recovery"]["checkpoint"],
                "seconds_until_caught_up_to_kill_point": resumed["replay_to_watermark_seconds"],
                "ledger_identical_to_uninterrupted_run": resumed["ledger_hash"] == res["ledger_hash"],
                "minutes_processed_total": resumed["minutes_processed"]}
            print(f"        recovery: identical={resumed['ledger_hash'] == res['ledger_hash']}, "
                  f"checkpoint load {resumed['recovery']['seconds']} s", flush=True)
    if not a.no_realtime:
        n = max(int(x) for x in a.sizes.split(","))
        rep["realtime"] = realtime(n, tmp / "rt", a, a.realtime_minutes)
        print("realtime:", rep["realtime"]["lag_s_after_minute_close"], "overruns", rep["realtime"]["overruns"], flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    out = Path(a.out or Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / f"screening_{datetime.now():%Y-%m-%d}.json")
    out.write_text(json.dumps(rep, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main()
