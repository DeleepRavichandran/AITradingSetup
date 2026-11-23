"""Root-cause analysis (RCA) for EVERY completed paper trade, winners and losers alike.

Standing rule (operator, 2026-11-23): every trade is documented with its cause. For each round trip
(buy -> sell) this module records the facts and a primary cause plus contributing causes in plain words.

Facts: exit reason (rule / stop / square-off), holding time, best and worst move while held (from the
stored 1-minute highs and lows), profit before and after charges, the market context at the buy (from the
trade journal when present).

HINDSIGHT: "recovered after the sale" looks at prices in the 15 minutes AFTER the exit. RCA is documentation
after the fact; nothing here is ever fed back into trading decisions.

Thresholds are explicit and provisional (listed below); they label trades, they do not prove causes.
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

from nseql.screening.trade_report import round_trips

RECOVERY_MIN = 15          # minutes after a stop-out to check whether price came back above the buy price
GAVE_BACK = 0.005          # was up at least 0.5% while held, then closed at a loss
WEAK_BREADTH = 0.4         # fewer than 40% of watchlist stocks up since the open

SCHEMA = """CREATE TABLE IF NOT EXISTS trade_rca (config_id TEXT, exit_ts TEXT, day TEXT, symbol TEXT, family TEXT, timeframe TEXT,
  stop_pct REAL, entry_ts TEXT, entry_px REAL, exit_px REAL, qty INTEGER, hold_min REAL, exit_reason TEXT, net_inr REAL, gross_inr REAL,
  charges_inr REAL, best_move REAL, worst_move REAL, recovered_after_exit INTEGER, regime TEXT, trend REAL, breadth REAL, news_60m INTEGER,
  outcome TEXT, primary_cause TEXT, causes TEXT, summary TEXT, PRIMARY KEY (config_id, exit_ts));
CREATE INDEX IF NOT EXISTS ix_rca_day ON trade_rca(day);"""


def _bars(db, sym: str, lo: str, hi: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT ts, high, low, close FROM events WHERE symbol = ? AND ts >= ? AND ts <= ? AND late = 0 ORDER BY ts",
                             db, params=(sym, lo, hi))


def _iso(t) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%S")


def classify(t: dict) -> tuple[str, str, list[str], str]:
    """-> (outcome, primary cause, contributing causes, one-line summary). Pure function of the facts."""
    net, gross, ch = t["net_inr"], t["gross_inr"], t["charges_inr"]
    ex, hold = t["exit_reason"], t["hold_min"]
    best, worst = t.get("best_move"), t.get("worst_move")
    stop = t["stop_pct"]
    held = f"held {hold:.0f} min" if hold is not None else "holding time unknown"
    contrib = []
    if t.get("trend") is not None and t["trend"] < 0:
        contrib.append("bought below the 5-minute 50-bar average (stock in a short-term downtrend)")
    if t.get("breadth") is not None and t["breadth"] < WEAK_BREADTH:
        contrib.append(f"bought while most watchlist stocks were down on the day (breadth {t['breadth']:.0%})")
    if t.get("regime") == "volatile" and stop <= 0.005:
        contrib.append(f"tight {stop:.1%} stop in a volatile market")
    if t.get("news_60m"):
        contrib.append(f"{t['news_60m']} headline(s) about the stock in the hour before the buy")
    if hold is not None and hold < 5:
        contrib.append("very short trade (under 5 min): costs are large relative to the move")
    if net > 0:
        cause = {"rule": "the strategy's sell signal came after a gain", "stop": "sold at the stop but still above costs",
                 "square-off": "held to the 15:15 square-off with a gain"}.get(ex, "closed with a gain")
        return "won", cause, contrib, f"Won {net:,.2f} INR after {ch:,.2f} charges; {cause}; {held}" + (f"; best move {best:+.2%}" if best is not None else "")
    if gross > 0:
        cause = "charges were larger than the gain"
    elif ex == "stop" and t.get("recovered_after_exit"):
        cause = f"stopped out by noise: hit the {stop:.1%} stop, then price recovered above the buy price within {RECOVERY_MIN} min (hindsight)"
    elif ex == "stop":
        cause = f"stop did its job: price kept falling after the {stop:.1%} stop"
    elif best is not None and best >= GAVE_BACK:
        cause = f"gave back a gain: was up {best:+.2%} while held, then closed lower"
    elif best is not None and best * t["entry_px"] * t["qty"] < ch:
        cause = "never moved up enough to cover the charges"
    elif ex == "square-off":
        cause = "intraday time ran out: squared off at 15:15 below the buy price"
    else:
        cause = "the strategy's sell signal came after a fall"
    return "lost", cause, contrib, f"Lost {abs(net):,.2f} INR ({gross:+,.2f} before {ch:,.2f} charges); {cause}; {held}" + (
        f"; best {best:+.2%}, worst {worst:+.2%}" if best is not None else "")


def analyse(db: sqlite3.Connection, day: str, write: bool = True) -> list[dict]:
    """RCA for every round trip closed on `day`. Writes the trade_rca table when `write`."""
    rt = round_trips(db, day)
    if rt.empty:
        return []
    has_journal = db.execute("SELECT 1 FROM sqlite_master WHERE name='journal'").fetchone() is not None
    out = []
    for r in rt.to_dict("records"):
        t = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()}
        t["day"] = day
        if t["entry_ts"] is not None and not pd.isna(t["entry_ts"]):
            b = _bars(db, t["symbol"], _iso(t["entry_ts"]), _iso(t["exit_ts"]))
            if len(b) and t["entry_px"]:
                t["best_move"] = float(b.high.max() / t["entry_px"] - 1)
                t["worst_move"] = float(b.low.min() / t["entry_px"] - 1)
            after = _bars(db, t["symbol"], _iso(t["exit_ts"]), _iso(pd.Timestamp(t["exit_ts"]) + pd.Timedelta(minutes=RECOVERY_MIN)))
            t["recovered_after_exit"] = int(bool(len(after)) and t["entry_px"] is not None and float(after.high.max()) > t["entry_px"])
            if has_journal:
                j = db.execute("SELECT regime, trend, breadth, news_60m FROM journal WHERE config_id = ? AND ts = ? AND side = 'BUY' LIMIT 1",
                               (t["config_id"], _iso(t["entry_ts"]))).fetchone()
                if j:
                    t["regime"], t["trend"], t["breadth"], t["news_60m"] = j
        t["outcome"], t["primary_cause"], contrib, t["summary"] = classify(t)
        t["causes"] = contrib
        out.append(t)
    if write:
        db.executescript(SCHEMA)
        cols = ("config_id", "exit_ts", "day", "symbol", "family", "timeframe", "stop_pct", "entry_ts", "entry_px", "exit_px", "qty", "hold_min",
                "exit_reason", "net_inr", "gross_inr", "charges_inr", "best_move", "worst_move", "recovered_after_exit", "regime", "trend",
                "breadth", "news_60m", "outcome", "primary_cause", "causes", "summary")
        val = lambda t, c: json.dumps(t[c]) if c == "causes" else (_iso(t[c]) if c in ("entry_ts", "exit_ts") and t.get(c) is not None else t.get(c))
        db.executemany(f"INSERT OR REPLACE INTO trade_rca ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [tuple(val(t, c) for c in cols) for t in out])
    return out


def summary(rcas: list[dict]) -> dict:
    by: dict[str, dict] = {}
    for t in rcas:
        key = t["primary_cause"].split(":")[0]
        g = by.setdefault(key, {"cause": key, "outcome": t["outcome"], "trades": 0, "net_inr": 0.0})
        g["trades"] += 1
        g["net_inr"] = round(g["net_inr"] + t["net_inr"], 2)
    return {"by_cause": sorted(by.values(), key=lambda g: g["net_inr"]),
            "won": sum(t["outcome"] == "won" for t in rcas), "lost": sum(t["outcome"] == "lost" for t in rcas)}
