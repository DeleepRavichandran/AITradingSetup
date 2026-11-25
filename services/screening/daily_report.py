"""End-of-day trade report: every trade, why it passed or failed, and what to do differently next time.

Built from the account's own ledger (round trips, fills, charges) and the per-trade root cause (rca.py), which uses the
market context recorded live in the trade journal. Nothing is replayed or estimated.

"Next time" is a list of concrete changes, each tied to the failure cause it addresses, the number of trades and rupees
it explains today, and the experiment that must pass before it goes live. One day is one sample: a change is applied
only after it has been tested on days it was not designed on (services/screening/experiment.py).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from nseql.screening.rca import analyse

# failure pattern -> (what it means, what to change, the test that decides)
ACTIONS = [
    ("costs", lambda t: t["primary_cause"] in ("charges were larger than the gain", "never moved up enough to cover the charges")
     or any("very short trade" in c for c in t["causes"]),
     "The move was too small to pay for the charges and the spread.",
     "Trade fewer, longer setups: drop the 1-minute strategies and keep 5- and 15-minute ones (fewer trades, bigger moves per trade).",
     "experiment: INC-L without 1-minute strategies vs INC-L, walk-forward + holdout"),
    ("noise_stop", lambda t: t["primary_cause"].startswith("stopped out by noise"),
     "The stop was hit by normal wiggles, then the price recovered.",
     "Use wider, volatility-based stops for these strategy types instead of fixed 0.5-1%.",
     "experiment: ATR-scaled stops vs fixed stops"),
    ("gave_back", lambda t: t["primary_cause"].startswith("gave back a gain"),
     "The trade was in profit but gave it all back.",
     "Add a trailing stop once a trade is up by about one day's typical move.",
     "experiment: trailing exit vs current exits"),
    ("opening", lambda t: t.get("entry_minute") is not None and t["entry_minute"] <= 30 and t["outcome"] == "lost",
     "Bought in the first 30 minutes, when prices are noisiest.",
     "No new entries before 09:46.",
     "experiment H2 (accepted on synthetic validation; to be confirmed on real days)"),
    ("downtrend", lambda t: any("short-term downtrend" in c for c in t["causes"]) and t["outcome"] == "lost",
     "Bought a stock that was already falling.",
     "Only buy when the stock is above its 5-minute trend line.",
     "experiment: trend filter vs no filter"),
    ("weak_market", lambda t: any("watchlist stocks were down" in c for c in t["causes"]) and t["outcome"] == "lost",
     "Bought while most of the market was falling.",
     "Skip new buys when fewer than 40% of watchlist stocks are up on the day.",
     "experiment: breadth filter vs no filter"),
    ("bearish_candle", lambda t: t.get("pa_pattern") in ("bearish engulfing", "shooting star", "red marubozu") and t["outcome"] == "lost",
     "Bought right after a bearish candle (sellers were in control on the chart).",
     "Do not buy right after a bearish engulfing, shooting star or strong red 5-minute candle.",
     "experiment: price-action entry filter vs no filter"),
    ("chasing", lambda t: str(t.get("pa_location", "")).startswith("near the day's high") and t["outcome"] == "lost",
     "Bought at the top of the day's range (chasing the move).",
     "Avoid buys in the top quarter of the day's range unless volume confirms a breakout.",
     "experiment: location filter vs no filter"),
    ("below_vwap", lambda t: t.get("pa_vwap") == "below VWAP" and t["outcome"] == "lost",
     "Bought while the price was below VWAP (the day's average buyer was losing).",
     "Prefer buys above VWAP.",
     "experiment: VWAP filter vs no filter"),
    ("down_structure", lambda t: str(t.get("pa_structure", "")).startswith("lower highs") and t["outcome"] == "lost",
     "Bought into lower highs and lower lows on the 5-minute chart.",
     "Wait for the structure to turn (a higher low) before buying.",
     "experiment: structure filter vs no filter"),
    ("time_out", lambda t: t["primary_cause"].startswith("intraday time ran out"),
     "Ran out of time and was sold at 15:15 at a loss.",
     "No new intraday entries after 14:30.",
     "experiment: earlier entry cut-off"),
]


def report(db: sqlite3.Connection, day: str) -> dict:
    rc = analyse(db, day, write=False)
    from nseql.screening.price_action import labels as pa_labels
    j, pa = {}, {}
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(journal)")}
        q = "SELECT config_id, ts, minute_of_session" + (", price_action" if "price_action" in cols else ", NULL") + " FROM journal WHERE side='BUY'"
        for cid, ts, mos, p in db.execute(q):
            j[(cid, ts)] = mos
            pa[(cid, ts)] = json.loads(p) if p else {}
    except sqlite3.Error:
        pass
    trades = []
    for t in sorted(rc, key=lambda t: str(t["exit_ts"])):
        ent = str(t["entry_ts"])[:19].replace(" ", "T") if t.get("entry_ts") is not None else None
        t["entry_minute"] = j.get((t["config_id"], ent))
        raw = pa.get((t["config_id"], ent), {})
        t.update(pa_labels(raw))
        trades.append({"symbol": t["symbol"], "strategy": f"{t['family']} {t['timeframe']}, stop {t['stop_pct']:.1%}",
                       "bought": str(t["entry_ts"])[11:16] if t.get("entry_ts") is not None else None, "buy_price": t.get("entry_px"),
                       "sold": str(t["exit_ts"])[11:16], "sell_price": t.get("exit_px"), "qty": t.get("qty"),
                       "held_min": None if t.get("hold_min") is None else round(t["hold_min"]),
                       "net_inr": round(t["net_inr"], 2), "charges_inr": round(t["charges_inr"], 2), "outcome": t["outcome"],
                       "why": t["primary_cause"], "also": t["causes"], "exit": t["exit_reason"],
                       "chart_at_buy": {"pattern": t["pa_pattern"], "structure": t["pa_structure"], "location": t["pa_location"],
                                        "vwap": t["pa_vwap"], "volume": t["pa_volume"]}})
    won = [t for t in rc if t["outcome"] == "won"]
    lost = [t for t in rc if t["outcome"] == "lost"]
    nxt = []
    for key, match, meaning, change, test in ACTIONS:
        hit = [t for t in rc if match(t)]
        if not hit:
            continue
        nxt.append({"pattern": key, "trades": len(hit), "lost_inr": round(-sum(min(t["net_inr"], 0) for t in hit), 2),
                    "net_inr": round(sum(t["net_inr"] for t in hit), 2), "meaning": meaning, "next_time": change, "decided_by": test})
    nxt.sort(key=lambda x: -x["lost_inr"])
    keep = []
    if won:
        by = {}
        for t in won:
            by.setdefault(f"{t['family']} {t['timeframe']}", []).append(t["net_inr"])
        best = sorted(by.items(), key=lambda kv: -sum(kv[1]))[:3]
        keep = [{"strategy_type": k, "wins": len(v), "won_inr": round(sum(v), 2)} for k, v in best]
    net = sum(t["net_inr"] for t in rc)
    ch = sum(t["charges_inr"] for t in rc)
    return {"day": day, "round_trips": len(rc), "won": len(won), "lost": len(lost), "net_inr": round(net, 2), "charges_inr": round(ch, 2),
            "gross_inr": round(net + ch, 2), "trades": trades, "next_time": nxt, "worked": keep,
            "plain": (f"{len(rc)} trades: {len(won)} won, {len(lost)} lost; net {net:+,.0f} INR after {ch:,.0f} INR charges "
                      f"({net + ch:+,.0f} before charges)." if rc else "No completed trades."),
            "rule": "A 'next time' change goes live only after it beats the current rules on days it was not designed on."}


def run(account: Path, day: str) -> dict:
    db = sqlite3.connect(f"file:{Path(account) / 'screening.sqlite'}?mode=ro", uri=True)
    db.row_factory = None
    try:
        return report(db, day)
    finally:
        db.close()
