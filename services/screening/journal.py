"""Trade journal: the market state around every paper fill (self-improvement step 1).

For each fill the journal stores what was known at the DECISION time, i.e. the engine state after the
previous minute, before this minute's bar is used (fills happen at this minute's open). Nothing later.

Per fill: minute of session; the stock's move since its open; trend (last close vs the 50-bar average on
5-minute bars); realised volatility over the last 60 one-minute returns, scaled to a session; the market
move (equal-weight average move since the open across the watchlist) and breadth (share of watchlist stocks
up since the open); a volatility regime label; headlines about the stock first seen in the previous 60 min.

The regime label uses fixed, provisional thresholds on the market's median session-scaled volatility:
calm < 0.8%, normal 0.8-1.6%, volatile > 1.6%. They are a starting point for the bleed report, not fitted.
The NIFTY index is not fetched any more (it is not traded); the market move is measured on the watchlist.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
import pandas as pd

from nseql.screening.features import SESSION_MINUTES, minute_of_session

CALM, VOLATILE = 0.008, 0.016
COLUMNS = ("fill_key", "ts", "config_id", "symbol", "side", "minute_of_session", "stock_move", "trend", "vol_session",
           "market_move", "breadth", "regime", "news_60m")


def context(engine) -> dict:
    """Per-symbol market state as of the last processed minute (call BEFORE stepping the next minute)."""
    fe, a = engine.fe, engine.x.alloc
    sess = fe.sess
    open_, last = sess[:, 1], sess[:, 4]
    with np.errstate(invalid="ignore", divide="ignore"):
        move = last / open_ - 1
        sma = fe.feat["5m"]["sma50"]
        trend = fe.last_close / sma - 1
    n = a.buf_n
    R = a.ret_buf[:, :n] if n else np.zeros((len(last), 0))
    vol = R.std(axis=1) * math.sqrt(SESSION_MINUTES) if n >= 10 else np.full(len(last), np.nan)
    ok = np.isfinite(move)
    mkt = float(np.nanmean(move)) if ok.any() else float("nan")
    breadth = float((move[ok] > 0).mean()) if ok.any() else float("nan")
    mvol = float(np.nanmedian(vol)) if np.isfinite(vol).any() else float("nan")
    regime = "unknown" if not math.isfinite(mvol) else "calm" if mvol < CALM else "volatile" if mvol > VOLATILE else "normal"
    return {"move": move, "trend": trend, "vol": vol, "market_move": mkt, "breadth": breadth, "regime": regime}


def rows(fills: list[tuple], ctx: dict, sym_index: dict[str, int], news_count: Callable[[str, pd.Timestamp], int] | None) -> list[tuple]:
    """fills: the scheduler's fill tuples (fill_key, order_key, config_id, symbol, side, ts, qty, price, ...)."""
    out = []
    f = lambda x: None if x is None or not math.isfinite(float(x)) else round(float(x), 6)
    for fk, _ok, cid, sym, side, ts, *_ in fills:
        k = sym_index.get(sym)
        t = pd.Timestamp(ts)
        news = None
        if news_count is not None:
            try:
                news = int(news_count(sym, t))
            except Exception:       # noqa: BLE001 - the journal must never stop trading
                news = None
        out.append((fk, ts, cid, sym, side, minute_of_session(t),
                    f(ctx["move"][k]) if k is not None else None, f(ctx["trend"][k]) if k is not None else None,
                    f(ctx["vol"][k]) if k is not None else None, f(ctx["market_move"]), f(ctx["breadth"]), ctx["regime"], news))
    return out
