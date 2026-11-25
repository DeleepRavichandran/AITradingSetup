"""Market-structure intelligence: ICT / Smart Money Concepts, classical price action, liquidity, Wyckoff, auction market
theory, volume profile and session behaviour, as reproducible quantitative definitions (docs/MARKET_STRUCTURE.md).

These are RESEARCH OBJECTS, not a trading methodology. Nothing here places or blocks an order, and no trading module imports
it (test-enforced). Whether any concept has value is decided by concept_study.py: after costs, against a simpler rule of the
same kind and against random timing, with multiple-testing correction, by regime and instrument, and stable over time.

Point in time, no repainting
  Every object carries `formed_at` (the candle that defines it) and `observable_at` (the first candle close at which the
  definition can be checked, e.g. a swing high needs K later candles). Detection walks candles in order and uses only
  candles up to the current one, so a chart drawn "as of" a time equals the detector run on data truncated at that time
  (tested). Later candles only ADD history (mitigated / invalidated), they never move or delete an earlier object.

What OHLCV candles can and cannot show
  Observed: open, high, low, close, volume per candle; levels built from them (previous-day high, opening range, VWAP).
  Inferred: every pattern (BOS, FVG, order block, sweep, spring, ...). A pattern is a geometric fact about candles, NOT
  evidence of institutional orders, stop clusters or intentions. Not available from Angel One 1-minute candles: order book,
  market depth, bid-ask spread, tick-by-tick trades, so no cumulative volume delta, footprint or liquidity heat map is
  produced. The spread used in simulation is a modelled assumption. Volume profile is approximated by spreading each
  1-minute bar's volume evenly over its range (not traded-at-price data).

All thresholds are fixed a priori here (PARAMS) and hashed when the concepts are registered; tuning them later is visible.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

VERSION = "ms-1.0"
PARAMS = {
    "pivot_internal": 2,       # a swing needs this many candles on each side (internal structure)
    "pivot_external": 5,       # external (major) structure / dealing range
    "atr_n": 20,               # average candle range used to scale sizes
    "displacement": 1.5,       # body >= 1.5 x average range, close in the outer 25% of the candle
    "equal_tol": 0.001,        # equal highs / lows within 0.1%
    "ote": (0.62, 0.79),       # optimal trade entry retracement band
    "premium_split": 0.5,      # premium above, discount below the dealing-range midpoint
    "false_break_bars": 3,     # a breakout that closes back inside within 3 candles is false
    "opening_range_min": 15,   # 09:15-09:30
    "wyckoff_range_atr": 4.0,  # a trading range no wider than 4 average ranges over 20 candles
    "bpr_bars": 10,            # opposite FVGs within 10 candles that overlap form a balanced price range
    "vol_high": 2.0,           # absorption-like: volume >= 2x average with range <= 0.5x average
    "vol_climax": 2.5,         # exhaustion-like: volume >= 2.5x average, range >= 2x average, at a 20-candle extreme
    "value_area": 0.70,
    "profile_bins": 60,
}
SESSIONS = (("open drive 09:15-10:00", 555, 600), ("morning 10:00-11:30", 600, 690), ("midday 11:30-13:30", 690, 810),
            ("afternoon 13:30-14:30", 810, 870), ("close 14:30-15:30", 870, 930))
UNAVAILABLE = ["order book and market depth", "bid-ask spread (simulation uses a modelled spread)", "tick-by-tick trades",
               "cumulative volume delta", "footprint charts", "liquidity heat maps", "institutional order flow"]

# concept -> (family, simpler rule of the same kind used as its baseline)
CONCEPTS = {
    "bos_up": ("ICT/SMC structure", "breakout_20_up"), "bos_down": ("ICT/SMC structure", "breakdown_20_down"),
    "choch_up": ("ICT/SMC structure", "breakout_20_up"), "choch_down": ("ICT/SMC structure", "breakdown_20_down"),
    "mss_up": ("ICT/SMC structure", "breakout_20_up"), "mss_down": ("ICT/SMC structure", "breakdown_20_down"),
    "cisd_up": ("ICT delivery", "three_up"), "cisd_down": ("ICT delivery", "three_down"),
    "displacement_up": ("ICT delivery", "three_up"), "displacement_down": ("ICT delivery", "three_down"),
    "fvg_bull": ("ICT imbalance", "three_up"), "fvg_bear": ("ICT imbalance", "three_down"),
    "fvg_bull_retest": ("ICT imbalance", "pullback_long"), "fvg_bear_retest": ("ICT imbalance", "pullback_short"),
    "ifvg_bear": ("ICT imbalance", "three_down"), "ifvg_bull": ("ICT imbalance", "three_up"),
    "bpr_long_retest": ("ICT imbalance", "pullback_long"), "bpr_short_retest": ("ICT imbalance", "pullback_short"),
    "fvg_bull_inv_retest": ("ICT imbalance", "pullback_long"), "fvg_bear_inv_retest": ("ICT imbalance", "pullback_short"),
    "ob_bull_retest": ("SMC blocks", "pullback_long"), "ob_bear_retest": ("SMC blocks", "pullback_short"),
    "breaker_bull_retest": ("SMC blocks", "pullback_long"), "breaker_bear_retest": ("SMC blocks", "pullback_short"),
    "mitigation_bull_retest": ("SMC blocks", "pullback_long"), "mitigation_bear_retest": ("SMC blocks", "pullback_short"),
    "sweep_ssl": ("liquidity", "new_low_reversal"), "sweep_bsl": ("liquidity", "new_high_reversal"),
    "inducement_ssl": ("liquidity", "new_low_reversal"), "inducement_bsl": ("liquidity", "new_high_reversal"),
    "false_breakout_up": ("classical price action", "new_high_reversal"), "false_breakout_down": ("classical price action", "new_low_reversal"),
    "ote_long": ("ICT retracement", "pullback_long"), "ote_short": ("ICT retracement", "pullback_short"),
    "orb_up": ("session", "breakout_20_up"), "orb_down": ("session", "breakdown_20_down"),
    "vwap_reclaim": ("session / VWAP", "three_up"), "vwap_loss": ("session / VWAP", "three_down"),
    "value_reentry_from_above": ("auction market theory", "new_high_reversal"), "value_reentry_from_below": ("auction market theory", "new_low_reversal"),
    "spring": ("Wyckoff", "new_low_reversal"), "upthrust": ("Wyckoff", "new_high_reversal"),
    "absorption_like_long": ("volume (inferred)", "new_low_reversal"), "absorption_like_short": ("volume (inferred)", "new_high_reversal"),
    "exhaustion_like_short": ("volume (inferred)", "new_high_reversal"), "exhaustion_like_long": ("volume (inferred)", "new_low_reversal"),
    "smt_bearish": ("ICT inter-market", None), "smt_bullish": ("ICT inter-market", None),
}
BASELINES = ("breakout_20_up", "breakdown_20_down", "three_up", "three_down", "pullback_long", "pullback_short", "new_low_reversal",
             "new_high_reversal")
DIRECTION = {**{k: (1 if k.endswith(("_up", "_bull", "_long", "_ssl", "bull_retest", "reclaim", "from_below", "spring", "_bullish"))
                    or k in ("fvg_bull", "spring", "smt_bullish") else -1) for k in CONCEPTS},
             "breakout_20_up": 1, "breakdown_20_down": -1, "three_up": 1, "three_down": -1, "pullback_long": 1, "pullback_short": -1,
             "new_low_reversal": 1, "new_high_reversal": -1, "ifvg_bull": 1, "ifvg_bear": -1, "sweep_bsl": -1, "inducement_bsl": -1,
             "upthrust": -1, "vwap_loss": -1, "value_reentry_from_above": -1, "false_breakout_up": -1}


def params_hash() -> str:
    return hashlib.sha256(json.dumps({"v": VERSION, "p": PARAMS, "c": CONCEPTS}, sort_keys=True, default=str).encode()).hexdigest()


def session_of(ts: pd.Timestamp) -> str:
    m = ts.hour * 60 + ts.minute
    return next((s for s, a, b in SESSIONS if a < m <= b), "outside session")


# ================================================================================================ volume profile
def profile(m1: pd.DataFrame, bins: int = PARAMS["profile_bins"]) -> dict | None:
    """Approximate volume profile: each 1-minute bar's volume spread evenly over [low, high]. POC = fullest bin; the value
    area grows from the POC to hold 70% of volume. An approximation from candles, not traded-at-price data."""
    if m1 is None or m1.empty or m1.volume.sum() <= 0:
        return None
    lo, hi = float(m1.low.min()), float(m1.high.max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for l, h, v in zip(m1.low.to_numpy(float), m1.high.to_numpy(float), m1.volume.to_numpy(float)):
        a, b = np.searchsorted(edges, l, "right") - 1, np.searchsorted(edges, h, "left")
        a, b = max(a, 0), min(max(b, a + 1), bins)
        vol[a:b] += v / (b - a)
    poc = int(np.argmax(vol))
    lo_i = hi_i = poc
    tot, need = vol[poc], PARAMS["value_area"] * vol.sum()
    while tot < need and (lo_i > 0 or hi_i < bins - 1):
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        dn = vol[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            tot += up
        else:
            lo_i -= 1
            tot += dn
    mid = (edges[:-1] + edges[1:]) / 2
    return {"poc": round(float(mid[poc]), 2), "vah": round(float(edges[hi_i + 1]), 2), "val": round(float(edges[lo_i]), 2),
            "method": "candle-range approximation (no traded-at-price data)"}


# ================================================================================================ the detector
def detect(c: pd.DataFrame, m1: pd.DataFrame | None = None, daily: pd.DataFrame | None = None, intraday: bool = True) -> dict:
    """Walk completed candles `c` (columns open, high, low, close, volume, end) in order. Returns events, zones (with their
    lifecycle) and liquidity levels. Every object uses only candles at or before the one it is observable on."""
    P = PARAMS
    n = len(c)
    out = {"events": [], "zones": [], "levels": [], "baselines": []}
    if n < 3:
        return out
    O, H, L, C, V = (c[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    T = pd.to_datetime(c.end).to_list()
    day = [t.normalize() for t in T]
    ki, ke = P["pivot_internal"], P["pivot_external"]
    sw_int, sw_ext = [], []                    # (index, price, kind) confirmed swings, in order of confirmation
    levels: list[dict] = []
    zones: list[dict] = []
    trend = None
    last_hi = last_lo = None                   # last unbroken confirmed internal swing high / low (dict)
    bear_run = bull_run = 0
    bear_run_open = bull_run_open = None
    pending_breaks: list[dict] = []
    ote_done: set = set()
    orh = orl = None
    or_broken = {1: False, -1: False}
    vwap_num = vwap_den = 0.0
    vwap_prev_side = None
    prev_profile = None
    opened_outside = None
    last_baseline = {}

    def atr(i: int) -> float:
        s = max(0, i - P["atr_n"])
        return float(np.mean(H[s:i] - L[s:i])) if i > s else float(H[i] - L[i]) or 1e-9

    def ev(kind, i, formed, price=None, zone=None, detail="", direction=None, **ctx):
        e = {"type": kind, "family": CONCEPTS.get(kind, ("", None))[0], "direction": DIRECTION.get(kind, 0) if direction is None else direction, "i": i,
             "formed_at": str(T[formed]), "observable_at": str(T[i]), "price": None if price is None else round(float(price), 2),
             "zone": None if zone is None else [round(float(zone[0]), 2), round(float(zone[1]), 2)], "detail": detail,
             "session": session_of(T[i]) if intraday else "daily", "inferred": True, "trend": trend, **ctx}
        out["events"].append(e)
        return e

    def base(kind, i):
        if last_baseline.get(kind, -99) < i - 4:       # at most one per 5 candles, like a sensible simple rule
            out["baselines"].append({"type": kind, "direction": DIRECTION[kind], "i": i, "observable_at": str(T[i]),
                                     "session": session_of(T[i]) if intraday else "daily"})
            last_baseline[kind] = i

    def add_level(kind, price, side, i, scope="internal"):
        lv = {"kind": kind, "price": round(float(price), 2), "side": side, "scope": scope, "observable_at": str(T[i]), "active": True,
              "taken_at": None, "how": None}
        levels.append(lv)
        return lv

    def add_zone(kind, lo, hi, i, formed, direction):
        z = {"kind": kind, "lo": round(float(min(lo, hi)), 2), "hi": round(float(max(lo, hi)), 2), "direction": direction,
             "formed_at": str(T[formed]), "observable_at": str(T[i]), "i": i, "mitigated_at": None, "invalidated_at": None, "history": []}
        zones.append(z)
        return z

    for i in range(n):
        born = len(out["events"])
        a = atr(i)
        new_day = intraday and (i == 0 or day[i] != day[i - 1])
        if new_day:
            vwap_num = vwap_den = 0.0
            vwap_prev_side = None
            orh = orl = None
            or_broken = {1: False, -1: False}
            opened_outside = None
            for lv in levels:                          # yesterday's session levels expire; external swings persist
                if lv["kind"] in ("opening range high", "opening range low", "VWAP", "session high", "session low"):
                    lv["active"] = False
            # previous-day / previous-week levels (observable at today's first candle)
            if daily is not None and len(daily):
                past = daily[pd.to_datetime(daily.ts) < day[i]]
                if len(past):
                    add_level("previous-day high", past.high.iloc[-1], "buy-side", i, "external")
                    add_level("previous-day low", past.low.iloc[-1], "sell-side", i, "external")
                    wk = past[pd.to_datetime(past.ts) >= day[i] - pd.Timedelta(days=day[i].weekday() + 7)]
                    wk = wk[pd.to_datetime(wk.ts) < day[i] - pd.Timedelta(days=day[i].weekday())]
                    if len(wk):
                        add_level("previous-week high", wk.high.max(), "buy-side", i, "external")
                        add_level("previous-week low", wk.low.min(), "sell-side", i, "external")
            if m1 is not None and len(m1):
                prev = m1[(m1.ts < day[i]) & (m1.ts >= day[i] - pd.Timedelta(days=7))]
                if len(prev):
                    last_day = prev.ts.dt.normalize().max()
                    prev_profile = profile(prev[prev.ts.dt.normalize() == last_day])
                    if prev_profile:
                        add_level("prior value-area high", prev_profile["vah"], "buy-side", i, "external")
                        add_level("prior value-area low", prev_profile["val"], "sell-side", i, "external")
                        add_level("prior point of control", prev_profile["poc"], "both", i, "external")
                        opened_outside = 1 if O[i] > prev_profile["vah"] else -1 if O[i] < prev_profile["val"] else 0

        # ---- 1. swings confirmed now (pivot j confirmed at i = j + k)
        for k, store, scope in ((ki, sw_int, "internal"), (ke, sw_ext, "external")):
            j = i - k
            if j - k >= 0 and (not intraday or day[j - k] == day[i] or scope == "external"):
                if H[j] > H[j - k:j].max() and H[j] >= H[j + 1:i + 1].max():
                    store.append({"i": j, "price": H[j], "kind": "high", "obs": i})
                    if scope == "internal":
                        prior = [s for s in sw_int[:-1] if s["kind"] == "high" and j - s["i"] <= 30]
                        if prior and abs(prior[-1]["price"] / H[j] - 1) <= P["equal_tol"] and H[prior[-1]["i"]:j].max() <= max(prior[-1]["price"], H[j]) * (1 + P["equal_tol"]):
                            add_level("equal highs", max(prior[-1]["price"], H[j]), "buy-side", i, "internal")
                        last_hi = {"i": j, "price": H[j], "broken": False}
                    add_level(f"{scope} swing high", H[j], "buy-side", i, scope)
                if L[j] < L[j - k:j].min() and L[j] <= L[j + 1:i + 1].min():
                    store.append({"i": j, "price": L[j], "kind": "low", "obs": i})
                    if scope == "internal":
                        prior = [s for s in sw_int[:-1] if s["kind"] == "low" and j - s["i"] <= 30]
                        if prior and abs(prior[-1]["price"] / L[j] - 1) <= P["equal_tol"] and L[prior[-1]["i"]:j].min() >= min(prior[-1]["price"], L[j]) * (1 - P["equal_tol"]):
                            add_level("equal lows", min(prior[-1]["price"], L[j]), "sell-side", i, "internal")
                        last_lo = {"i": j, "price": L[j], "broken": False}
                    add_level(f"{scope} swing low", L[j], "sell-side", i, scope)

        body = abs(C[i] - O[i])
        rng = max(H[i] - L[i], 1e-9)
        disp_up = C[i] > O[i] and body >= P["displacement"] * a and (H[i] - C[i]) <= 0.25 * rng
        disp_dn = C[i] < O[i] and body >= P["displacement"] * a and (C[i] - L[i]) <= 0.25 * rng
        if disp_up:
            ev("displacement_up", i, i, C[i], detail=f"body {body / a:.1f}x average range")
        if disp_dn:
            ev("displacement_down", i, i, C[i], detail=f"body {body / a:.1f}x average range")

        # ---- 2. zones: lifecycle of earlier zones with this candle (mitigation, invalidation, inversion)
        for z in zones:
            if z["i"] >= i or z["invalidated_at"]:
                continue
            if z["direction"] > 0:
                if z["mitigated_at"] is None and L[i] <= z["hi"]:
                    z["mitigated_at"] = str(T[i])
                    z["history"].append({"at": str(T[i]), "event": "first retest"})
                    if C[i] >= z["lo"]:
                        ev(f"{z['kind']}_retest", i, z["i"], C[i], (z["lo"], z["hi"]), direction=z["direction"],
                           detail=f"first return into a {z['kind'].replace('_', ' ')} zone formed {z['formed_at'][5:16]}")
                if C[i] < z["lo"]:
                    z["invalidated_at"] = str(T[i])
                    z["history"].append({"at": str(T[i]), "event": "invalidated: closed below the zone"})
                    if z["kind"] == "fvg_bull":
                        add_zone("fvg_bear_inv", z["lo"], z["hi"], i, i, -1)
                        ev("ifvg_bear", i, z["i"], C[i], (z["lo"], z["hi"]), detail="bullish gap failed: now an inverse (bearish) gap")
                    if z["kind"] == "ob_bull":
                        add_zone("breaker_bear", z["lo"], z["hi"], i, i, -1)
            else:
                if z["mitigated_at"] is None and H[i] >= z["lo"]:
                    z["mitigated_at"] = str(T[i])
                    z["history"].append({"at": str(T[i]), "event": "first retest"})
                    if C[i] <= z["hi"]:
                        ev(f"{z['kind']}_retest", i, z["i"], C[i], (z["lo"], z["hi"]), direction=z["direction"],
                           detail=f"first return into a {z['kind'].replace('_', ' ')} zone formed {z['formed_at'][5:16]}")
                if C[i] > z["hi"]:
                    z["invalidated_at"] = str(T[i])
                    z["history"].append({"at": str(T[i]), "event": "invalidated: closed above the zone"})
                    if z["kind"] == "fvg_bear":
                        add_zone("fvg_bull_inv", z["lo"], z["hi"], i, i, 1)
                        ev("ifvg_bull", i, z["i"], C[i], (z["lo"], z["hi"]), detail="bearish gap failed: now an inverse (bullish) gap")
                    if z["kind"] == "ob_bear":
                        add_zone("breaker_bull", z["lo"], z["hi"], i, i, 1)

        # ---- 3. fair value gaps formed by candles i-2, i-1, i
        if i >= 2 and (not intraday or day[i - 2] == day[i]):
            if L[i] > H[i - 2] and C[i - 1] > O[i - 1]:
                z = add_zone("fvg_bull", H[i - 2], L[i], i, i - 1, 1)
                ev("fvg_bull", i, i - 1, C[i], (z["lo"], z["hi"]), detail=("liquidity void: " if L[i] - H[i - 2] >= a else "") + f"gap {L[i] - H[i - 2]:.2f}")
                opp = [y for y in zones if y["kind"] == "fvg_bear" and i - y["i"] <= P["bpr_bars"] and y["lo"] < z["hi"] and z["lo"] < y["hi"]]
                if opp:
                    add_zone("bpr_long", max(z["lo"], opp[-1]["lo"]), min(z["hi"], opp[-1]["hi"]), i, i - 1, 1)
            if H[i] < L[i - 2] and C[i - 1] < O[i - 1]:
                z = add_zone("fvg_bear", H[i], L[i - 2], i, i - 1, -1)
                ev("fvg_bear", i, i - 1, C[i], (z["lo"], z["hi"]), detail=("liquidity void: " if L[i - 2] - H[i] >= a else "") + f"gap {L[i - 2] - H[i]:.2f}")
                opp = [y for y in zones if y["kind"] == "fvg_bull" and i - y["i"] <= P["bpr_bars"] and y["lo"] < z["hi"] and z["lo"] < y["hi"]]
                if opp:
                    add_zone("bpr_short", max(z["lo"], opp[-1]["lo"]), min(z["hi"], opp[-1]["hi"]), i, i - 1, -1)

        # ---- 4. structure: BOS / CHoCH / MSS, order blocks, breaker / mitigation blocks
        if last_hi and not last_hi["broken"] and C[i] > last_hi["price"]:
            last_hi["broken"] = True
            kind = "bos_up" if trend in (None, "up") else "choch_up"
            ev(kind, i, last_hi["i"], C[i], detail=f"closed above swing high {last_hi['price']:.2f}")
            if kind == "choch_up" and disp_up:
                ev("mss_up", i, last_hi["i"], C[i], detail="change of character with displacement")
            lo_i = max([s["i"] for s in sw_int if s["kind"] == "low" and s["i"] < i] or [max(0, i - 10)])
            downs = [k for k in range(lo_i, i) if C[k] < O[k]]
            if downs:
                k = downs[-1]
                add_zone("ob_bull", L[k], H[k], i, k, 1)
            if kind == "choch_up":
                lows = [s for s in sw_int if s["kind"] == "low" and s["i"] < i]
                swept = len(lows) >= 2 and lows[-1]["price"] < lows[-2]["price"]
                ups = [k for k in range(max(0, lows[-1]["i"] - 10) if lows else max(0, i - 10), lows[-1]["i"] if lows else i) if C[k] > O[k]]
                if ups:
                    k = ups[-1]
                    add_zone("breaker_bull" if swept else "mitigation_bull", L[k], H[k], i, k, 1)
            trend = "up"
        if last_lo and not last_lo["broken"] and C[i] < last_lo["price"]:
            last_lo["broken"] = True
            kind = "bos_down" if trend in (None, "down") else "choch_down"
            ev(kind, i, last_lo["i"], C[i], detail=f"closed below swing low {last_lo['price']:.2f}")
            if kind == "choch_down" and disp_dn:
                ev("mss_down", i, last_lo["i"], C[i], detail="change of character with displacement")
            hi_i = max([s["i"] for s in sw_int if s["kind"] == "high" and s["i"] < i] or [max(0, i - 10)])
            ups = [k for k in range(hi_i, i) if C[k] > O[k]]
            if ups:
                k = ups[-1]
                add_zone("ob_bear", L[k], H[k], i, k, -1)
            if kind == "choch_down":
                highs = [s for s in sw_int if s["kind"] == "high" and s["i"] < i]
                swept = len(highs) >= 2 and highs[-1]["price"] > highs[-2]["price"]
                downs = [k for k in range(max(0, highs[-1]["i"] - 10) if highs else max(0, i - 10), highs[-1]["i"] if highs else i) if C[k] < O[k]]
                if downs:
                    k = downs[-1]
                    add_zone("breaker_bear" if swept else "mitigation_bear", L[k], H[k], i, k, -1)
            trend = "down"

        # ---- 5. CISD: close beyond the open of the run of opposite candles that preceded
        if bear_run >= 2 and C[i] > bear_run_open:
            ev("cisd_up", i, i, C[i], detail=f"closed above the open {bear_run_open:.2f} of {bear_run} falling candles")
        if bull_run >= 2 and C[i] < bull_run_open:
            ev("cisd_down", i, i, C[i], detail=f"closed below the open {bull_run_open:.2f} of {bull_run} rising candles")
        if C[i] < O[i]:
            bear_run_open = O[i] if bear_run == 0 else bear_run_open
            bear_run, bull_run = bear_run + 1, 0
        elif C[i] > O[i]:
            bull_run_open = O[i] if bull_run == 0 else bull_run_open
            bull_run, bear_run = bull_run + 1, 0

        # ---- 6. liquidity: sweeps, breakouts, false breakouts, inducement, Wyckoff spring / upthrust
        ext_above = any(lv["active"] and lv["side"] == "buy-side" and lv["scope"] == "external" and lv["price"] > H[i] for lv in levels)
        ext_below = any(lv["active"] and lv["side"] == "sell-side" and lv["scope"] == "external" and lv["price"] < L[i] for lv in levels)
        rng20_lo, rng20_hi = (L[max(0, i - 20):i].min(), H[max(0, i - 20):i].max()) if i >= 20 else (None, None)
        tight = rng20_lo is not None and (rng20_hi - rng20_lo) <= P["wyckoff_range_atr"] * a
        for lv in levels:
            if not lv["active"] or lv["observable_at"] >= str(T[i]):
                continue
            p = lv["price"]
            if lv["side"] in ("buy-side", "both") and H[i] > p and (i == 0 or H[i - 1] <= p):
                lv["active"], lv["taken_at"] = False, str(T[i])
                if C[i] < p:
                    lv["how"] = "swept"
                    ev("sweep_bsl", i, i, p, detail=f"took {lv['kind']} {p:.2f}, closed back below", level=lv["kind"], scope=lv["scope"])
                    if lv["scope"] == "internal" and ext_above:
                        ev("inducement_bsl", i, i, p, detail="internal buy-side liquidity taken while external liquidity lies above",
                           level=lv["kind"])
                    if tight and abs(p - rng20_hi) <= 0.25 * a:
                        ev("upthrust", i, i, p, detail="swept the top of a tight 20-candle range and closed back inside")
                else:
                    lv["how"] = "broken"
                    pending_breaks.append({"dir": 1, "price": p, "i": i, "kind": lv["kind"]})
            if lv["side"] in ("sell-side", "both") and L[i] < p and (i == 0 or L[i - 1] >= p):
                lv["active"], lv["taken_at"] = False, str(T[i])
                if C[i] > p:
                    lv["how"] = "swept"
                    ev("sweep_ssl", i, i, p, detail=f"took {lv['kind']} {p:.2f}, closed back above", level=lv["kind"], scope=lv["scope"])
                    if lv["scope"] == "internal" and ext_below:
                        ev("inducement_ssl", i, i, p, detail="internal sell-side liquidity taken while external liquidity lies below",
                           level=lv["kind"])
                    if tight and abs(p - rng20_lo) <= 0.25 * a:
                        ev("spring", i, i, p, detail="swept the bottom of a tight 20-candle range and closed back inside")
                else:
                    lv["how"] = "broken"
                    pending_breaks.append({"dir": -1, "price": p, "i": i, "kind": lv["kind"]})
        for b in list(pending_breaks):
            if i - b["i"] > P["false_break_bars"]:
                pending_breaks.remove(b)
            elif i > b["i"] and ((b["dir"] > 0 and C[i] < b["price"]) or (b["dir"] < 0 and C[i] > b["price"])):
                ev("false_breakout_up" if b["dir"] > 0 else "false_breakout_down", i, b["i"], b["price"],
                   detail=f"broke {b['kind']} {b['price']:.2f}, closed back inside within {i - b['i']} candle(s)")
                pending_breaks.remove(b)

        # ---- 7. dealing range: premium / discount and OTE
        hi_e = [s for s in sw_ext if s["kind"] == "high"]
        lo_e = [s for s in sw_ext if s["kind"] == "low"]
        pd_zone = None
        if hi_e and lo_e and hi_e[-1]["price"] > lo_e[-1]["price"]:
            pos = (C[i] - lo_e[-1]["price"]) / (hi_e[-1]["price"] - lo_e[-1]["price"])
            pd_zone = "premium" if pos > P["premium_split"] else "discount"
        hs = [s for s in sw_int if s["kind"] == "high"]
        ls = [s for s in sw_int if s["kind"] == "low"]
        if trend == "up" and hs and ls and ls[-1]["i"] < hs[-1]["i"]:
            lo_p, hi_p = ls[-1]["price"], hs[-1]["price"]
            a1, a2 = hi_p - P["ote"][0] * (hi_p - lo_p), hi_p - P["ote"][1] * (hi_p - lo_p)
            key = ("L", ls[-1]["i"], hs[-1]["i"])
            if key not in ote_done and L[i] <= a1 and C[i] >= a2 and i > hs[-1]["obs"]:
                ote_done.add(key)
                ev("ote_long", i, hs[-1]["i"], C[i], (a2, a1), detail="retraced 62-79% of the last up leg")
        if trend == "down" and hs and ls and hs[-1]["i"] < ls[-1]["i"]:
            hi_p, lo_p = hs[-1]["price"], ls[-1]["price"]
            a1, a2 = lo_p + P["ote"][0] * (hi_p - lo_p), lo_p + P["ote"][1] * (hi_p - lo_p)
            key = ("S", hs[-1]["i"], ls[-1]["i"])
            if key not in ote_done and H[i] >= a1 and C[i] <= a2 and i > ls[-1]["obs"]:
                ote_done.add(key)
                ev("ote_short", i, ls[-1]["i"], C[i], (a1, a2), detail="retraced 62-79% of the last down leg")

        # ---- 8. session: opening range, VWAP, auction (value-area re-entry)
        if intraday:
            mins = T[i].hour * 60 + T[i].minute
            tp = (H[i] + L[i] + C[i]) / 3
            vwap_num += tp * V[i]
            vwap_den += V[i]
            vw = vwap_num / vwap_den if vwap_den > 0 else None
            if mins <= 555 + P["opening_range_min"]:
                orh = H[i] if orh is None else max(orh, H[i])
                orl = L[i] if orl is None else min(orl, L[i])
                if mins == 555 + P["opening_range_min"]:
                    add_level("opening range high", orh, "buy-side", i, "internal")
                    add_level("opening range low", orl, "sell-side", i, "internal")
            elif orh is not None:
                if not or_broken[1] and C[i] > orh:
                    or_broken[1] = True
                    ev("orb_up", i, i, C[i], detail=f"first close above the opening range high {orh:.2f}")
                if not or_broken[-1] and C[i] < orl:
                    or_broken[-1] = True
                    ev("orb_down", i, i, C[i], detail=f"first close below the opening range low {orl:.2f}")
            if vw is not None:
                side = 1 if C[i] > vw else -1
                if vwap_prev_side == -1 and side == 1 and i > 0 and day[i - 1] == day[i]:
                    ev("vwap_reclaim", i, i, C[i], detail=f"closed back above session VWAP {vw:.2f}", vwap=round(vw, 2))
                if vwap_prev_side == 1 and side == -1 and i > 0 and day[i - 1] == day[i]:
                    ev("vwap_loss", i, i, C[i], detail=f"closed back below session VWAP {vw:.2f}", vwap=round(vw, 2))
                vwap_prev_side = side
            if prev_profile and opened_outside:
                if opened_outside > 0 and C[i] < prev_profile["vah"]:
                    ev("value_reentry_from_above", i, i, C[i], detail=f"opened above prior value, closed back inside (VAH {prev_profile['vah']})")
                    opened_outside = 0
                elif opened_outside < 0 and C[i] > prev_profile["val"]:
                    ev("value_reentry_from_below", i, i, C[i], detail=f"opened below prior value, closed back inside (VAL {prev_profile['val']})")
                    opened_outside = 0

        # ---- 9. volume behaviour (inferred from candles only)
        if i >= 20:
            av = float(np.mean(V[i - 20:i])) or 1e-9
            move6 = C[i] - C[i - 6]
            if V[i] >= P["vol_high"] * av and rng <= 0.5 * a and move6 != 0:
                ev("absorption_like_long" if move6 < 0 else "absorption_like_short", i, i, C[i],
                   detail=f"volume {V[i] / av:.1f}x average on a narrow candle after a {'fall' if move6 < 0 else 'rise'} (inferred)")
            if V[i] >= P["vol_climax"] * av and rng >= 2 * a:
                if C[i] > O[i] and H[i] >= H[i - 20:i].max():
                    ev("exhaustion_like_short", i, i, C[i], detail=f"climactic volume {V[i] / av:.1f}x at a 20-candle high (inferred)")
                if C[i] < O[i] and L[i] <= L[i - 20:i].min():
                    ev("exhaustion_like_long", i, i, C[i], detail=f"climactic volume {V[i] / av:.1f}x at a 20-candle low (inferred)")

        # ---- 10. simpler baselines (the same kind of idea, without the methodology's vocabulary)
        if i >= 20:
            if C[i] > H[i - 20:i].max():
                base("breakout_20_up", i)
            if C[i] < L[i - 20:i].min():
                base("breakdown_20_down", i)
            if L[i] < L[i - 20:i].min() and C[i] > O[i]:
                base("new_low_reversal", i)
            if H[i] > H[i - 20:i].max() and C[i] < O[i]:
                base("new_high_reversal", i)
            r_lo, r_hi = L[i - 20:i].min(), H[i - 20:i].max()
            ma = float(np.mean(C[i - 20:i]))
            if C[i - 1] > ma and L[i] <= r_hi - 0.5 * (r_hi - r_lo) and C[i] > O[i]:
                base("pullback_long", i)
            if C[i - 1] < ma and H[i] >= r_lo + 0.5 * (r_hi - r_lo) and C[i] < O[i]:
                base("pullback_short", i)
        if i >= 3:
            if C[i] > C[i - 1] > C[i - 2] > C[i - 3]:
                base("three_up", i)
            if C[i] < C[i - 1] < C[i - 2] < C[i - 3]:
                base("three_down", i)

        for e in out["events"][born:]:
            e["trend_after"] = trend                     # structure state after this candle (includes its own break)
            e["premium_discount"] = pd_zone

    out["zones"] = zones
    out["levels"] = levels
    out["trend"] = trend
    return out


def smt(a: pd.DataFrame, b: pd.DataFrame, name_b: str) -> list[dict]:
    """SMT divergence: A makes a higher confirmed swing high while B (a correlated instrument, aligned by candle end) does
    not, or the mirror at lows. Observable when A's swing is confirmed."""
    k = PARAMS["pivot_internal"]
    m = a.merge(b[["end", "high", "low"]], on="end", suffixes=("", "_b"))
    out = []
    if len(m) < 2 * k + 3:
        return out
    H, L, Hb, Lb = (m[x].to_numpy(float) for x in ("high", "low", "high_b", "low_b"))
    T = pd.to_datetime(m.end).to_list()
    hs, ls = [], []
    for i in range(2 * k, len(m)):
        j = i - k
        if H[j] > H[j - k:j].max() and H[j] >= H[j + 1:i + 1].max():
            if hs and H[j] > hs[-1][1] and Hb[j - k:i + 1].max() < hs[-1][2]:
                out.append({"type": "smt_bearish", "family": "ICT inter-market", "direction": -1, "i": i, "formed_at": str(T[j]),
                            "observable_at": str(T[i]), "price": round(H[j], 2), "detail": f"higher high not confirmed by {name_b}",
                            "inferred": True, "session": session_of(T[i])})
            hs.append((j, H[j], Hb[j - k:i + 1].max()))
        if L[j] < L[j - k:j].min() and L[j] <= L[j + 1:i + 1].min():
            if ls and L[j] < ls[-1][1] and Lb[j - k:i + 1].min() > ls[-1][2]:
                out.append({"type": "smt_bullish", "family": "ICT inter-market", "direction": 1, "i": i, "formed_at": str(T[j]),
                            "observable_at": str(T[i]), "price": round(L[j], 2), "detail": f"lower low not confirmed by {name_b}",
                            "inferred": True, "session": session_of(T[i])})
            ls.append((j, L[j], Lb[j - k:i + 1].min()))
    return out


def po3(m1_day: pd.DataFrame) -> dict | None:
    """Power of Three as a DESCRIPTIVE label of a finished session (observable only at the close; never a signal):
    accumulation near the open, a manipulation leg against the day's direction in the first hour, then distribution."""
    if m1_day is None or len(m1_day) < 120:
        return None
    o, c, h, l = float(m1_day.open.iloc[0]), float(m1_day.close.iloc[-1]), float(m1_day.high.max()), float(m1_day.low.min())
    first = m1_day.iloc[:60]
    rng = max(h - l, 1e-9)
    if c > o:
        manip = (o - float(first.low.min())) / rng
        return {"direction": "up", "manipulation_share": round(manip, 3), "label": "PO3 up" if manip >= 0.15 and (c - l) / rng >= 0.75 else "no PO3 shape",
                "observable_at": str(m1_day.ts.iloc[-1])}
    manip = (float(first.high.max()) - o) / rng
    return {"direction": "down", "manipulation_share": round(manip, 3), "label": "PO3 down" if manip >= 0.15 and (h - c) / rng >= 0.75 else "no PO3 shape",
            "observable_at": str(m1_day.ts.iloc[-1])}


# ================================================================================================ point-in-time chart annotation
def load_m1(db, symbol: str, upto: pd.Timestamp, days: int = 10) -> pd.DataFrame:
    m1 = pd.read_sql_query("SELECT ts, open, high, low, close, volume FROM events WHERE symbol=? AND late=0 AND ts >= ? AND ts <= ? ORDER BY ts",
                           db, params=(symbol, (upto.normalize() - pd.Timedelta(days=days)).strftime("%Y-%m-%d"), upto.strftime("%Y-%m-%dT%H:%M:%S")))
    m1["ts"] = pd.to_datetime(m1.ts)
    return m1


def daily_from_m1(m1: pd.DataFrame) -> pd.DataFrame:
    if m1.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    g = m1.groupby(m1.ts.dt.normalize())
    return pd.DataFrame({"ts": g.ts.first().dt.normalize(), "open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                         "close": g.close.last(), "volume": g.volume.sum()}).reset_index(drop=True)


def annotate(db, symbol: str, as_of, tf: int = 5) -> dict:
    """Structure as it stood at `as_of`: only completed candles at or before it (the same function the study uses)."""
    from nseql.screening.price_action import candles
    as_of = pd.Timestamp(as_of)
    m1 = load_m1(db, symbol, as_of)
    m1 = m1[m1.ts <= as_of]
    if m1.empty:
        return {"symbol": symbol, "as_of": str(as_of), "events": [], "zones": [], "levels": [], "unavailable": UNAVAILABLE}
    c = candles(m1, tf)
    c = c[c.end <= as_of].reset_index(drop=True)
    d = detect(c, m1=m1, daily=daily_from_m1(m1))
    day = as_of.normalize()
    htf = detect(candles(m1, 15).pipe(lambda x: x[x.end <= as_of].reset_index(drop=True)), daily=None)
    today = [e for e in d["events"] if pd.Timestamp(e["observable_at"]) >= day]
    zones = [z for z in d["zones"] if pd.Timestamp(z["observable_at"]) >= day - pd.Timedelta(days=1)]
    levels = [lv for lv in d["levels"] if lv["active"] or (lv["taken_at"] and pd.Timestamp(lv["taken_at"]) >= day)]
    return {"symbol": symbol, "as_of": str(as_of), "timeframe": f"{tf}m", "trend": d.get("trend"), "htf_trend_15m": htf.get("trend"),
            "events": today[-80:], "zones": zones[-40:], "levels": [lv for lv in levels if "swing" not in lv["kind"] or lv["scope"] == "external"][-30:],
            "profile_today": profile(m1[m1.ts >= day]), "po3_yesterday": po3(m1[(m1.ts < day) & (m1.ts >= day - pd.Timedelta(days=4))].pipe(
                lambda x: x[x.ts.dt.normalize() == x.ts.dt.normalize().max()] if len(x) else x)),
            "params_hash": params_hash()[:12], "version": VERSION, "unavailable": UNAVAILABLE,
            "labels": {"observed": "candles, volume, VWAP, previous-day/week levels, opening range",
                       "inferred": "every pattern, zone and liquidity level (geometry of candles, not institutional intent)",
                       "validated": "only what concept_study.py shows after costs; see each concept's verdict"}}
