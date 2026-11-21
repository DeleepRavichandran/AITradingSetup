"""Registered strategy configurations for the live screening loop (intraday + swing).

A configuration = rule family + parameters + timeframe + symbol + risk overlay. Its id is a hash of
those fields, so the same configuration has the same id on every machine and every restart. The
registry is columnar (numpy arrays) so the engine evaluates all configurations of a timeframe with a
handful of vector operations instead of one Python call each.

Per symbol: 19 entry/exit rules x (3 intraday timeframes x 2 stops + 1 daily timeframe x 3 stops) =
171 configurations; 300 symbols give 51,300, of which the first N by id are registered.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from nseql.screening.features import CHAN_N, RSI_N, SMA_W, TIMEFRAMES

FAMILIES = ("cross", "breakout", "rsi")
TF_NAMES = tuple(TIMEFRAMES)                     # 1m 5m 15m 1d
HORIZON = {"1m": "intraday", "5m": "intraday", "15m": "intraday", "1d": "swing"}
RULES = ([("cross", f, s) for f, s in ((5, 20), (10, 50), (20, 50), (20, 100), (50, 200))]
         + [("breakout", e, x) for e, x in ((10, 10), (20, 10), (20, 20), (55, 10), (55, 20))]
         + [("rsi", n, (b, s)) for n in RSI_N for b, s in ((10, 50), (20, 60), (30, 70))])
STOPS = {"intraday": (0.005, 0.01), "swing": (0.03, 0.05, 0.08)}
FEATURES = ["close", *(f"sma{w}" for w in SMA_W), *(f"hh{n}" for n in CHAN_N), *(f"ll{n}" for n in CHAN_N), *(f"rsi{n}" for n in RSI_N)]
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


def build(symbols: list[str], n: int | None = None) -> Registry:
    rows = []
    for si, s in enumerate(symbols):
        for tf in TF_NAMES:
            hz = HORIZON[tf]
            for fam, a, b in RULES:
                for stop in STOPS[hz]:
                    if fam == "cross":
                        p = {"fast": a, "slow": b}
                        en = (FIDX[f"sma{a}"], FIDX[f"sma{b}"], 0.0)                 # fast > slow
                        ex = (FIDX[f"sma{b}"], FIDX[f"sma{a}"], 0.0)                 # slow >= fast  (exit when not fast > slow)
                    elif fam == "breakout":
                        p = {"entry": a, "exit": b}
                        en = (FIDX["close"], FIDX[f"hh{a}"], 0.0)                    # close > prior high
                        ex = (FIDX[f"ll{b}"], FIDX["close"], 0.0)                    # close < prior low
                    else:
                        p = {"n": a, "buy": b[0], "sell": b[1]}
                        en = (-1, FIDX[f"rsi{a}"], float(b[0]))                      # buy level > rsi
                        ex = (FIDX[f"rsi{a}"], -1, float(b[1]))                      # rsi > sell level
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
