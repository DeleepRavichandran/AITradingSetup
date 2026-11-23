"""Registered strategy configurations for the live screening loop (intraday + swing).

A configuration = rule family + parameters + timeframe + symbol + risk overlay. Its id is a hash of
those fields, so the same configuration has the same id on every machine and every restart. The
registry is columnar (numpy arrays) so the engine evaluates all configurations of a timeframe with a
handful of vector operations instead of one Python call each.

Per symbol: 36 entry/exit rules in 12 families. The 30 rules that work on any timeframe run on 3 intraday
timeframes x 2 stops + the daily timeframe x 3 stops; the 6 session rules (VWAP, opening range, gap,
previous-day high) run on the 3 intraday timeframes only: 30 x 9 + 6 x 6 = 306 configurations per symbol;
300 symbols give 91,800, of which the first N by id are registered.
Families: cross, breakout, rsi (since 2026-11); zscore, bollinger, macd, momentum, volume, vwap, orb, gap,
prevday (added 2026-11-23).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np
from nseql.screening.features import FEATURE_NAMES, MACD_SET, ROC_N, TIMEFRAMES, VBO_N

REGISTRY_VERSION = 2          # bump when rules change: saved runs of another version are not resumed
FAMILIES = ("cross", "breakout", "rsi", "zscore", "bollinger", "macd", "momentum", "volume", "vwap", "orb", "gap", "prevday")
SESSION_FAMILIES = {"vwap", "orb", "gap", "prevday"}            # intraday timeframes only
TF_NAMES = tuple(TIMEFRAMES)                     # 1m 5m 15m 1d
HORIZON = {"1m": "intraday", "5m": "intraday", "15m": "intraday", "1d": "swing"}
RULES = ([("cross", f, s) for f, s in ((5, 20), (10, 50), (20, 50), (20, 100), (50, 200))]
         + [("breakout", e, x) for e, x in ((10, 10), (20, 10), (20, 20), (55, 10), (55, 20))]
         + [("rsi", n, (b, s)) for n in (2, 5, 14) for b, s in ((10, 50), (20, 60), (30, 70))]
         + [("zscore", k, 0.0) for k in (-1.5, -2.0, -2.5)] + [("bollinger", 20, 2)]
         + [("macd", f, sl) for f, sl in MACD_SET] + [("momentum", n, k) for n, k in zip(ROC_N, (0.0, 0.005, 0.01))]
         + [("volume", n, 1.5) for n in VBO_N] + [("vwap", k, 0.0) for k in (-0.003, -0.006)] + [("orb", 15, 0)]
         + [("gap", "go", 0.005), ("gap", "fill", -0.005)] + [("prevday", 0.0, -0.005)])
STOPS = {"intraday": (0.005, 0.01), "swing": (0.03, 0.05, 0.08)}
FEATURES = list(FEATURE_NAMES)
FIDX = {k: i for i, k in enumerate(FEATURES)}


def config_id(family: str, tf: str, params: dict, symbol: str, stop: float) -> str:
    body = json.dumps([family, tf, sorted(params.items()), symbol, round(stop, 6)], separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass
class Registry:
    ids: np.ndarray          # str16
    sym: np.ndarray          # int32 symbol index
    tf: np.ndarray           # int8 index into TF_NAMES
    fam: np.ndarray          # int8 index into FAMILIES
    f_enter_a: np.ndarray    # feature row compared in the entry rule (left side)
    f_enter_b: np.ndarray    # right side feature row, or -1 for a constant
    k_enter: np.ndarray      # constant for rsi entry
    f_exit_a: np.ndarray
    f_exit_b: np.ndarray
    k_exit: np.ndarray
    stop: np.ndarray         # stop distance as a fraction of the fill price
    intraday: np.ndarray     # bool
    params: list[dict]
    symbols: list[str]

    def __len__(self) -> int:
        return len(self.ids)

    def describe(self, i: int) -> dict:
        tf = TF_NAMES[self.tf[i]]
        return {"id": str(self.ids[i]), "index": int(i), "symbol": self.symbols[self.sym[i]], "timeframe": tf, "horizon": HORIZON[tf],
                "family": FAMILIES[self.fam[i]], "params": self.params[i], "stop_pct": float(self.stop[i])}


def encode(fam: str, a, b) -> tuple[dict, tuple, tuple]:
    """(params, entry, exit). A rule is "left > right": (feature, feature, 0), (-1, feature, k) for k > feature,
    (feature, -1, k) for feature > k."""
    F = FIDX
    if fam == "cross":                                               # fast SMA above slow; exit when it is not
        return {"fast": a, "slow": b}, (F[f"sma{a}"], F[f"sma{b}"], 0.0), (F[f"sma{b}"], F[f"sma{a}"], 0.0)
    if fam == "breakout":                                            # close above prior high; exit below prior low
        return {"entry": a, "exit": b}, (F["close"], F[f"hh{a}"], 0.0), (F[f"ll{b}"], F["close"], 0.0)
    if fam == "rsi":                                                 # RSI below the buy level; exit above the sell level
        return {"n": a, "buy": b[0], "sell": b[1]}, (-1, F[f"rsi{a}"], float(b[0])), (F[f"rsi{a}"], -1, float(b[1]))
    if fam == "zscore":                                              # z below the entry level; exit when z > b
        return {"z_entry": a, "z_exit": b}, (-1, F["z20"], float(a)), (F["z20"], -1, float(b))
    if fam == "bollinger":                                           # close above the upper band; exit below the middle
        return {"n": a, "k": b}, (F["close"], F["bbu20"], 0.0), (F["sma20"], F["close"], 0.0)
    if fam == "macd":                                                # MACD above its signal; exit below
        return {"fast": a, "slow": b, "signal": 9}, (F[f"macd_{a}_{b}"], F[f"macds_{a}_{b}"], 0.0), (F[f"macds_{a}_{b}"], F[f"macd_{a}_{b}"], 0.0)
    if fam == "momentum":                                            # n-bar return above k; exit when it turns negative
        return {"n": a, "min_return": b}, (F[f"roc{a}"], -1, float(b)), (-1, F[f"roc{a}"], 0.0)
    if fam == "volume":                                              # high-volume breakout; exit below the 10-bar low
        return {"n": a, "volume_x": b}, (F[f"vbo{a}"], -1, 0.5), (F["ll10"], F["close"], 0.0)
    if fam == "vwap":                                                # close below VWAP by more than |a|; exit above VWAP
        return {"below_vwap": a}, (-1, F["vwapdev"], float(a)), (F["vwapdev"], -1, 0.0)
    if fam == "orb":                                                 # above the opening-range high; exit below its middle
        return {"minutes": a}, (F["orb_up"], -1, 0.0), (-1, F["orb_mid"], 0.0)
    if fam == "gap":
        if a == "go":                                                # gap up and trading above the open; exit below the open
            return {"kind": "gap-and-go", "min_gap": b}, (F["gapgo"], -1, float(b)), (-1, F["dayret"], 0.0)
        return {"kind": "gap-fill", "max_gap": b}, (-1, F["gapdn"], float(b)), (F["gapfill"], -1, 0.0)   # gap down; exit when filled
    if fam == "prevday":                                             # above yesterday's high; exit 0.5% back below it
        return {"above": a, "exit_below": b}, (F["pdh_dev"], -1, float(a)), (-1, F["pdh_dev"], float(b))
    raise ValueError(fam)


def build(symbols: list[str], n: int | None = None) -> Registry:
    rows = []
    for si, s in enumerate(symbols):
        for tf in TF_NAMES:
            hz = HORIZON[tf]
            for fam, a, b in RULES:
                if fam in SESSION_FAMILIES and hz != "intraday":
                    continue
                p, en, ex = encode(fam, a, b)
                for stop in STOPS[hz]:
                    rows.append((config_id(fam, tf, p, s, stop), si, TF_NAMES.index(tf), FAMILIES.index(fam), en, ex, stop, hz == "intraday", p))
    rows.sort(key=lambda r: r[0])
    if n is not None:
        if n > len(rows):
            raise ValueError(f"{n} configurations requested but {len(symbols)} symbols only give {len(rows)}")
        rows = rows[:n]
    col = lambda k: [r[k] for r in rows]
    en, ex = np.array(col(4), dtype=float), np.array(col(5), dtype=float)
    return Registry(np.array(col(0)), np.array(col(1), dtype=np.int32), np.array(col(2), dtype=np.int8), np.array(col(3), dtype=np.int8),
                    en[:, 0].astype(np.int16), en[:, 1].astype(np.int16), en[:, 2], ex[:, 0].astype(np.int16), ex[:, 1].astype(np.int16), ex[:, 2],
                    np.array(col(6)), np.array(col(7), dtype=bool), col(8), list(symbols))
