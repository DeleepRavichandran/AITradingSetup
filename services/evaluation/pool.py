"""High-throughput evaluation workers.

Worker processes are pure numerical functions: they read the shared indicator cache
(memory-mapped, read-only), evaluate a chunk of configurations and write rows straight into
shared memory-mapped result matrices. They import no LLM client, no network library, no
registry and nothing from the execution or broker packages.

The scheduler here is a local process pool. The unit of work (a list of configuration indices
plus paths) is serialisable, so the same worker function can be driven by a distributed queue;
that backend is NOT IMPLEMENTED.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from nseql.evaluation import cache as cache_mod
from nseql.evaluation.space import universe
from nseql.evaluation.vector import evaluate


@dataclass
class EvalBudget:
    max_configs: int = 50_000
    max_seconds: float = 1800.0
    worker_memory_mb: int = 3072
    workers: int = 0                 # 0 = all CPUs
    chunk: int = 256


class BudgetExceeded(RuntimeError):
    pass


_P = None
_OUT = None


def _init(cache_path: str, out_path: str, rows: int, n_cfg: int, mem_mb: int):
    global _P, _OUT
    import resource
    lim = mem_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    except (ValueError, OSError):
        pass
    _P = cache_mod.load(Path(cache_path))
    _OUT = {k: np.load(Path(out_path) / f"{k}.npy", mmap_mode="r+") for k in ("G", "R", "E")}


def _work(args):
    idx, cost = args
    cfgs = universe()
    t0 = time.perf_counter()
    for i in idx:
        g, r, e = evaluate(cfgs[i], _P, cost)
        _OUT["G"][i] = g
        _OUT["R"][i] = r
        _OUT["E"][i] = e
    for m in _OUT.values():
        m.flush()
    return len(idx), time.perf_counter() - t0


def evaluate_universe(cache_path: Path, out_dir: Path, cost: float, indices: list[int] | None = None,
                      budget: EvalBudget | None = None, progress=None) -> dict:
    """Evaluate configurations into out_dir/{G,R,E}.npy (rows = configuration index). Idempotent per
    (cache, cost): rows already marked done are skipped."""
    budget = budget or EvalBudget()
    P = cache_mod.load(cache_path)
    T = int(P["ts"].shape[0])
    n_all = len(universe())
    idx = list(range(n_all)) if indices is None else list(indices)
    if len(idx) > budget.max_configs:
        raise BudgetExceeded(f"{len(idx)} configurations requested, budget is {budget.max_configs}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for k, dt in (("G", np.float32), ("R", np.float32), ("E", np.float16)):
        f = out_dir / f"{k}.npy"
        if not f.exists():
            np.lib.format.open_memmap(f, mode="w+", dtype=dt, shape=(n_all, T)).flush()
    done_f = out_dir / "done.npy"
    done = np.load(done_f) if done_f.exists() else np.zeros(n_all, dtype=bool)
    todo = [i for i in idx if not done[i]]
    workers = budget.workers or os.cpu_count() or 1
    chunks = [todo[i:i + budget.chunk] for i in range(0, len(todo), budget.chunk)]
    t0 = time.perf_counter()
    cpu = 0.0
    n_done = 0
    if chunks:
        ctx = mp.get_context("forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn")   # never fork a threaded parent
        with ctx.Pool(workers, initializer=_init, initargs=(str(cache_path), str(out_dir), T, n_all, budget.worker_memory_mb)) as pool:
            for (n, secs), ch in zip(pool.imap(_work, [(c, cost) for c in chunks]), chunks):
                done[ch] = True
                n_done += n
                cpu += secs
                if progress:
                    progress(n_done, len(todo))
                if time.perf_counter() - t0 > budget.max_seconds:
                    pool.terminate()
                    np.save(done_f, done)
                    raise BudgetExceeded(f"wall-clock budget {budget.max_seconds}s exceeded after {n_done} configurations")
        np.save(done_f, done)
    wall = time.perf_counter() - t0
    stats = {"requested": len(idx), "evaluated_now": n_done, "already_done": len(idx) - len(todo), "workers": workers,
             "wall_seconds": round(wall, 2), "cpu_seconds": round(cpu, 2),
             "configs_per_second": round(n_done / wall, 1) if wall > 0 and n_done else None, "rows": T, "cost_per_side": cost,
             "budget": asdict(budget)}
    (out_dir / "stats.json").write_text(json.dumps(stats))
    return stats


def load_results(out_dir: Path) -> dict:
    return {k: np.load(Path(out_dir) / f"{k}.npy", mmap_mode="r") for k in ("G", "R", "E", "done")}
