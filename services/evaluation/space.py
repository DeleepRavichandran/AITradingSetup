"""The strategy-configuration universe screened by the high-throughput workers.

A configuration = base rule (family + parameters) x overlays (market-regime filter, per-symbol
volatility filter, universe subset, position sizing). The enumeration is deterministic, so a
configuration's integer index and its `key` are stable across runs and machines.

    5 families, 518 parameter sets  x  3 regime x 4 vol x 4 universe x 2 sizing overlays  =  49,728
    optional context extension:  518 x 8 context/event states x 2 vol x 2 universe x 2 sizing  =  33,152
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

BASE: dict[str, list[dict]] = {
    "trend": [{"fast": f, "slow": s} for f in (5, 10, 15, 20, 30, 40, 50) for s in (40, 60, 80, 100, 120, 150, 200, 250) if 2 * f <= s],
    "momentum": [{"lookback": lb, "skip": sk, "top_k": k, "rebalance": rb}
                 for lb in (20, 40, 60, 90, 120, 180, 250) for sk in (0, 5, 10) for k in (2, 3, 4, 5, 6) for rb in (5, 10, 21)],
    "breakout": [{"entry": e, "exit": x} for e in (10, 20, 40, 55, 80, 100) for x in (5, 10, 20, 40) if x < e],
    "meanrev": [{"n": n, "z_in": zi, "z_out": zo} for n in (5, 10, 20, 30, 60) for zi in (-1.0, -1.5, -2.0, -2.5) for zo in (-0.5, 0.0, 0.5)],
    "rsi": [{"n": n, "buy": b, "sell": s} for n in (2, 3, 5, 7, 14) for b in (10, 15, 20, 25, 30) for s in (50, 60, 70)],
}
REGIME = ("none", "sma100", "sma200")          # trade only while the index is above its N-day average
VOLCAP = (None, 0.20, 0.30, 0.40)              # skip symbols whose 20-day realised vol exceeds the cap
UNIVERSE = ("all", "liquid", "lowvol", "highvol")
SIZING = ("equal", "invvol")
FACETS = ("family", "horizon", "regime", "vol", "universe", "sizing")

# Optional market-context extension (needs a context dataset; see nseql.data.context). Each context
# state is a regime filter; to keep the extension small it uses a reduced grid of the
# other overlays (8 x 4,144 = 33,152). Its indices continue after the base universe, so base results are unaffected.
CONTEXT = ("ctx_vix_calm", "ctx_no_vix_spike", "ctx_broad", "ctx_inr_stable", "ctx_crude_calm",
           "ctx_no_results", "ctx_no_macro_event", "ctx_no_expiry")
CTX_VOLCAP = (None, 0.30)
CTX_UNIVERSE = ("all", "liquid")


def horizon(family: str, p: dict) -> str:
    n = {"trend": p.get("slow"), "momentum": p.get("lookback"), "breakout": p.get("entry"), "meanrev": p.get("n"), "rsi": p.get("n")}[family]
    return "short" if n <= 20 else "medium" if n <= 80 else "long"


@dataclass(frozen=True)
class Config:
    index: int
    family: str
    params: tuple                    # sorted (name, value) pairs
    regime: str
    vol: float | None
    universe: str
    sizing: str

    @property
    def p(self) -> dict:
        return dict(self.params)

    @property
    def key(self) -> str:
        body = json.dumps([self.family, self.params, self.regime, self.vol, self.universe, self.sizing], separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    @property
    def lookback(self) -> int:
        p = self.p
        return {"trend": p.get("slow", 0), "momentum": p.get("lookback", 0) + p.get("skip", 0), "breakout": p.get("entry", 0),
                "meanrev": p.get("n", 0), "rsi": p.get("n", 0) * 10}[self.family]

    def facets(self) -> dict:
        return {"family": self.family, "horizon": horizon(self.family, self.p), "regime": self.regime,
                "vol": "none" if self.vol is None else f"<{self.vol:.2f}", "universe": self.universe, "sizing": self.sizing}

    def describe(self) -> dict:
        return {"index": self.index, "key": self.key, "family": self.family, "params": self.p, "regime_filter": self.regime,
                "vol_cap": self.vol, "universe": self.universe, "sizing": self.sizing, "facets": self.facets()}


@lru_cache(maxsize=2)
def universe(context: bool = False) -> tuple[Config, ...]:
    """The base universe (49,728), or base + the market-context extension (82,880) when context=True."""
    out = []
    for fam in sorted(BASE):
        for p in BASE[fam]:
            for rg in REGIME:
                for v in VOLCAP:
                    for u in UNIVERSE:
                        for sz in SIZING:
                            out.append(Config(len(out), fam, tuple(sorted(p.items())), rg, v, u, sz))
    if context:
        for fam in sorted(BASE):
            for p in BASE[fam]:
                for rg in CONTEXT:
                    for v in CTX_VOLCAP:
                        for u in CTX_UNIVERSE:
                            for sz in SIZING:
                                out.append(Config(len(out), fam, tuple(sorted(p.items())), rg, v, u, sz))
    return tuple(out)


def size(context: bool = False) -> int:
    return len(universe(context))


@lru_cache(maxsize=1)
def twins() -> dict[int, int]:
    """Context configuration index -> the identical base configuration without the context filter."""
    base = {(c.family, c.params, c.vol, c.universe, c.sizing): c.index for c in universe() if c.regime == "none"}
    return {c.index: base[(c.family, c.params, c.vol, c.universe, c.sizing)] for c in universe(True)[len(universe()):]}


@lru_cache(maxsize=1)
def facet_table() -> tuple[dict[str, list[str]], dict[str, "object"]]:
    """(values per facet, integer code array per facet over the whole universe) for fast vector ops."""
    import numpy as np
    cfgs = universe()
    values: dict[str, list[str]] = {}
    codes: dict[str, np.ndarray] = {}
    for f in FACETS:
        col = [c.facets()[f] for c in cfgs]
        vals = sorted(set(col))
        idx = {v: i for i, v in enumerate(vals)}
        values[f] = vals
        codes[f] = np.array([idx[v] for v in col], dtype=np.int16)
    return values, codes
