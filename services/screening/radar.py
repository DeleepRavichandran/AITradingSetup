"""Opportunity radar, decision dossier and decision stream for the dashboard (VEGA). Read-only views of the live ledger.

Every figure is what the bot recorded at decision time: no outcome is used to rank or to describe a decision (the
hindsight scores live only in the research store, see opportunities.py). Ranking is by decision state, then by the
best expected edge after costs that the allocator recorded for the stock today (its score, in basis points), never by
signal count.

States, per stock, for the latest session:
  HOLD       a paper position is open
  TAKEN      bought today, position closed since
  REFUSED    the strategies wanted to buy; every buy was refused (the most common recorded reason is shown)
  WATCH      no buy signal yet today
and a portfolio-wide BLOCKED reason when new buys are impossible (kill switch, daily loss limit, trade limit, opening
blackout, stale data, market closed).
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from nseql.screening.metrics import _next_day, reason_category


def _rules_names(sch):
    from nseql.screening.registry import FAMILIES, TF_NAMES
    from nseql.screening.universe import _rule_name
    idx = {str(i): k for k, i in enumerate(sch.reg.ids)}

    def rule(cid: str) -> tuple[str, str]:
        k = idx.get(cid)
        if k is None:
            return "strategy", ""
        return _rule_name(FAMILIES[sch.reg.fam[k]], sch.reg.params[k]), TF_NAMES[sch.reg.tf[k]]
    return rule


def session_day(db) -> str | None:
    r = db.execute("SELECT substr(MAX(minute),1,10) FROM cycles WHERE status='processed' AND mode != 'warmup'").fetchone()
    return r[0] if r and r[0] else None


def limits(sch, now: datetime | None, kill: bool, market_open: bool, last_minute: str | None) -> dict:
    """Risk and execution usage against the account's own rules (PortfolioRules), and whether new buys are possible."""
    x = sch.engine.x
    snap = x.snapshot()
    r = x.rules
    eq = float(snap["equity_inr"]) or 1.0
    out = {"rules": r.to_dict() if r else None, "entries_today": snap["entries_today"], "entries_allowed": snap["entries_allowed_today"]}
    if r:
        top = snap["top_symbols"][0] if snap["top_symbols"] else None
        loss = max(0.0, (float(snap["day_start_equity_inr"]) - eq) / max(float(snap["day_start_equity_inr"]), 1.0))
        out["usage"] = [
            {"name": "Gross exposure", "used": round(snap["gross_exposure_inr"] / eq, 4), "limit": r.max_gross_pct, "unit": "pct"},
            {"name": "Open positions", "used": snap["open_positions"], "limit": r.max_positions, "unit": "count"},
            {"name": "Largest stock", "used": round(top["pct_equity"], 4) if top else 0.0, "limit": r.max_symbol_pct, "unit": "pct",
             "detail": top["symbol"] if top else None},
            {"name": "Daily loss", "used": round(loss, 4), "limit": r.daily_loss_pct, "unit": "pct"},
        ] + ([{"name": "New trades today", "used": snap["entries_today"], "limit": snap["entries_allowed_today"], "unit": "count"}]
             if snap["entries_allowed_today"] else [])
    blocked = []
    if kill:
        blocked.append("kill switch engaged (exits continue)")
    if snap["halted_today"]:
        blocked.append("daily loss limit reached")
    if snap["entries_allowed_today"] and snap["entries_today"] >= snap["entries_allowed_today"]:
        blocked.append("daily trade limit reached (more needs the operator's approval)")
    if not market_open:
        blocked.append("market closed")
    if now is not None and r and r.no_entry_first_minutes and market_open:
        mins = now.hour * 60 + now.minute - (9 * 60 + 15)
        if 0 <= mins < r.no_entry_first_minutes + 1:
            blocked.append(f"opening blackout: no new buys before {(9 * 60 + 16 + r.no_entry_first_minutes) // 60:02d}:"
                           f"{(9 * 60 + 16 + r.no_entry_first_minutes) % 60:02d}")
    if now is not None and last_minute and market_open:
        age = (pd.Timestamp(now) - pd.Timestamp(last_minute)).total_seconds()
        if age > 180:
            blocked.append(f"no fresh prices for {int(age // 60)} minutes")
    out["blocked"] = blocked
    out["new_buys_possible"] = not blocked
    out["snapshot"] = {k: snap[k] for k in ("equity_inr", "cash_inr", "reserved_inr", "day_start_equity_inr", "gross_exposure_inr",
                                             "open_positions", "working_orders", "halted_today")}
    return out


def coverage(db, day: str | None) -> dict:
    if not day:
        return {"minute": None}
    r = db.execute("SELECT minute, symbols_with_data, symbols_expected, lag_s, latency_ms FROM cycles WHERE minute >= ? AND minute < ? "
                   "AND status='processed' ORDER BY minute DESC LIMIT 1", (day, _next_day(day))).fetchone()
    q = db.execute("SELECT COUNT(*), SUM(CASE WHEN mode='catch-up' THEN 1 ELSE 0 END), AVG(latency_ms) FROM cycles WHERE minute >= ? AND minute < ? "
                   "AND status='processed' AND mode != 'warmup'", (day, _next_day(day))).fetchone()
    p95 = db.execute("SELECT latency_ms FROM cycles WHERE minute >= ? AND minute < ? AND status='processed' AND mode != 'warmup' "
                     "ORDER BY latency_ms", (day, _next_day(day))).fetchall()
    return {"minute": r[0] if r else None, "symbols_with_data": r[1] if r else None, "symbols_expected": r[2] if r else None,
            "lag_s": r[3] if r else None, "minutes": q[0] or 0, "catch_up_minutes": q[1] or 0,
            "latency_p95_ms": round(float(p95[int(0.95 * (len(p95) - 1))][0]), 1) if p95 else None}


def radar(sch, meta: dict, positions: list[dict], now: datetime | None, kill: bool, market_open: bool) -> dict:
    db = sch.store.reader() if hasattr(sch.store, "reader") else sch.store.db
    day = session_day(db)
    rule = _rules_names(sch)
    live_from = meta.get("live_from") or ""
    lim = limits(sch, now, kill, market_open, coverage(db, day)["minute"])
    symbols = [s for s in sch.reg.symbols if s not in ("NIFTY50", "NIFTYBANK", "INDIAVIX")]
    rows = {s: {"symbol": s, "state": "WATCH", "signals": 0, "accepted": 0, "refused": 0, "top_reason": None, "best_edge_bps": None,
                "best_setup": None, "last_signal": None, "buys": 0, "sells": 0, "realised_inr": 0.0, "position": None,
                "price": None, "day_change": None} for s in symbols}
    if day:
        lo, hi = max(day, live_from), _next_day(day)
        sig = db.execute("SELECT symbol, config_id, bar_ts, alloc_status, alloc_reason, alloc_score FROM signals WHERE decision='BUY' "
                         "AND bar_ts >= ? AND bar_ts < ?", (lo, hi)).fetchall()
        reasons: dict[str, dict[str, int]] = {}
        for sym, cid, ts, status, why, score in sig:
            row = rows.get(sym)
            if row is None:
                continue
            cat = "accepted" if status == "accepted" else reason_category(why)
            if cat in ("warm-up history", "catch-up (late data, not traded)"):
                continue
            row["signals"] += 1
            if status == "accepted":
                row["accepted"] += 1
            else:
                row["refused"] += 1
                reasons.setdefault(sym, {})[cat] = reasons.get(sym, {}).get(cat, 0) + 1
            if row["last_signal"] is None or ts > row["last_signal"]:
                row["last_signal"] = ts
            if score is not None and (row["best_edge_bps"] is None or score > row["best_edge_bps"]):
                row["best_edge_bps"] = round(float(score), 2)
                rn, tf = rule(cid)
                row["best_setup"] = f"{rn} · {tf}" if tf else rn
        for sym, d in reasons.items():
            rows[sym]["top_reason"] = max(d.items(), key=lambda z: z[1])[0]
            rows[sym]["reasons"] = dict(sorted(d.items(), key=lambda z: -z[1]))
        for sym, side, n, rp in db.execute("SELECT symbol, side, COUNT(*), COALESCE(SUM(realized_paise),0) FROM paper_orders "
                                           "WHERE status IN ('filled','partial') AND fill_ts >= ? AND fill_ts < ? GROUP BY 1, 2", (day, hi)):
            if sym in rows:
                rows[sym]["buys" if side == "BUY" else "sells"] = n
                if side == "SELL":
                    rows[sym]["realised_inr"] = round(rp / 100, 2)
        for sym, op, cl in db.execute("SELECT symbol, (SELECT open FROM events e2 WHERE e2.symbol=e.symbol AND e2.ts >= ? AND e2.ts < ? AND late=0 "
                                      "ORDER BY ts LIMIT 1), (SELECT close FROM events e3 WHERE e3.symbol=e.symbol AND e3.ts >= ? AND e3.ts < ? "
                                      "AND late=0 ORDER BY ts DESC LIMIT 1) FROM (SELECT DISTINCT symbol FROM events WHERE ts >= ? AND ts < ?) e",
                                      (day, hi, day, hi, day, hi)):
            if sym in rows and cl is not None:
                rows[sym]["price"] = round(float(cl), 2)
                rows[sym]["day_change"] = round(float(cl) / float(op) - 1, 5) if op else None
    for p in positions:
        row = rows.get(p["symbol"])
        if row is not None:
            q = (row["position"] or {"qty": 0, "value_inr": 0.0, "unrealised_inr": 0.0, "entry_price": p["entry_price"], "stop_price": p["stop_price"]})
            q["qty"] += p["qty"]
            q["value_inr"] = round(q["value_inr"] + p["value_inr"], 2)
            q["unrealised_inr"] = round(q["unrealised_inr"] + p["unrealised_inr"], 2)
            q["rule"] = p.get("rule")
            row["position"] = q
    for row in rows.values():
        row["state"] = ("HOLD" if row["position"] else "TAKEN" if row["buys"] else "REFUSED" if row["signals"] and not row["accepted"] else
                        "WATCH")
        row["why"] = (f"position open ({row['position']['qty']} shares, {row['position'].get('rule') or 'strategy'})" if row["state"] == "HOLD" else
                      f"bought {row['buys']}x today; closed" if row["state"] == "TAKEN" else
                      f"{row['refused']} buy signal(s) refused: {row['top_reason']}" if row["state"] == "REFUSED" else
                      "no buy signal today")
    order = {"HOLD": 0, "TAKEN": 1, "REFUSED": 2, "WATCH": 3}
    ranked = sorted(rows.values(), key=lambda r: (order[r["state"]], -(r["best_edge_bps"] if r["best_edge_bps"] is not None else -1e9), r["symbol"]))
    return {"day": day, "live_from": live_from or None, "rows": ranked, "limits": lim, "coverage": coverage(db, day),
            "counts": {k: sum(r["state"] == k for r in ranked) for k in order},
            "ranking": "decision state, then the best expected edge after costs the allocator recorded today (basis points); never signal count",
            "note": "decisions as recorded at the time; no later outcome is used here"}


def dossier(sch, meta: dict, symbol: str, positions: list[dict], now: datetime | None, kill: bool, market_open: bool) -> dict:
    """Everything behind one stock's decision: portfolio gates, per-stock gates, the chart read at the last candle,
    today's signals with their recorded reasons and scores, orders, and the account's past evidence in this stock."""
    from nseql.screening.chart_analysis import analyse
    db = sch.store.reader() if hasattr(sch.store, "reader") else sch.store.db
    if symbol not in sch.reg.symbols:
        raise KeyError(symbol)
    day = session_day(db)
    rule = _rules_names(sch)
    r = sch.engine.x.rules
    rad = radar(sch, meta, positions, now, kill, market_open)
    row = next(x for x in rad["rows"] if x["symbol"] == symbol)
    lo = max(day or "", meta.get("live_from") or "")
    hi = _next_day(day) if day else ""
    sig = db.execute("SELECT config_id, bar_ts, alloc_status, alloc_reason, alloc_score, price FROM signals WHERE symbol=? AND decision='BUY' "
                     "AND bar_ts >= ? AND bar_ts < ? ORDER BY bar_ts DESC LIMIT 400", (symbol, lo, hi)).fetchall() if day else []
    signals = []
    for cid, ts, st, why, sc, px in sig:
        cat = "accepted" if st == "accepted" else reason_category(why)
        if cat in ("warm-up history", "catch-up (late data, not traded)"):
            continue
        rn, tf = rule(cid)
        signals.append({"time": ts[11:16], "setup": rn, "timeframe": tf, "status": st, "category": cat, "reason": why,
                        "edge_bps": None if sc is None else round(float(sc), 2), "price": px})
    orders = [{"time": (f or d or "")[11:16], "side": s, "status": st, "qty": q, "price": p, "net_inr": None if rp is None or s == "BUY" else rp / 100,
               "charges_inr": (ch or 0) / 100, "setup": rule(c)[0]} for c, s, st, q, p, f, d, ch, rp in db.execute(
        "SELECT config_id, side, status, qty, price, fill_ts, decided_at, charges_paise, realized_paise FROM paper_orders WHERE symbol=? "
        "AND decided_at >= ? AND decided_at < ? ORDER BY decided_at DESC LIMIT 50", (symbol, day or "", hi))] if day else []
    hist = db.execute("SELECT COUNT(*), COALESCE(SUM(realized_paise),0), SUM(CASE WHEN realized_paise > 0 THEN 1 ELSE 0 END) FROM paper_orders "
                      "WHERE symbol=? AND side='SELL' AND status IN ('filled','partial')", (symbol,)).fetchone()
    last_exit = db.execute("SELECT MAX(fill_ts) FROM paper_orders WHERE symbol=? AND side='SELL' AND status IN ('filled','partial') AND fill_ts >= ?",
                           (symbol, day or "")).fetchone()[0]
    gates = [{"gate": "Portfolio accepts new buys", "ok": rad["limits"]["new_buys_possible"],
              "detail": "; ".join(rad["limits"]["blocked"]) or "no portfolio-wide block"}]
    if r:
        if r.max_open_per_symbol:
            gates.append({"gate": "One position per stock", "ok": row["position"] is None,
                          "detail": "already holding" if row["position"] else "not holding"})
        if r.max_entries_per_symbol_day:
            gates.append({"gate": f"At most {r.max_entries_per_symbol_day} buys in this stock today", "ok": row["buys"] < r.max_entries_per_symbol_day,
                          "detail": f"{row['buys']} so far"})
        if r.cooldown_minutes and last_exit and now is not None:
            free = pd.Timestamp(last_exit) + pd.Timedelta(minutes=r.cooldown_minutes)
            gates.append({"gate": f"{r.cooldown_minutes}-minute cool-down after a sale", "ok": pd.Timestamp(now) >= free,
                          "detail": f"last sale {str(last_exit)[11:16]}; free from {free:%H:%M}"})
    gates.append({"gate": "Positive expected edge after costs (allocator)", "ok": (row["best_edge_bps"] or 0) > 0,
                  "detail": "no buy signal today" if row["best_edge_bps"] is None else f"best {row['best_edge_bps']:+.1f} bps today"})
    chart = None
    if sch.watermark:
        try:
            chart = analyse(db, symbol, pd.Timestamp(sch.watermark))
        except Exception as e:      # noqa: BLE001 - shown, never breaks the dossier
            chart = {"read": [f"Chart read unavailable: {type(e).__name__}"]}
    cats: dict[str, int] = {}
    for s in signals:
        cats[s["category"]] = cats.get(s["category"], 0) + 1
    return {"symbol": symbol, "day": day, "row": row, "gates": gates, "chart": chart, "signals": signals[:60], "signal_categories": cats,
            "orders": orders, "history": {"round_trips": hist[0], "net_inr": hist[1] / 100, "won": hist[2] or 0},
            "limits": rad["limits"], "note": "recorded at decision time; no later outcome is used to describe these decisions"}


def stream(sch, meta: dict, limit: int = 40) -> dict:
    """What the bot did and why, newest first: fills, refused buys (grouped per stock and minute) and portfolio events."""
    db = sch.store.reader() if hasattr(sch.store, "reader") else sch.store.db
    day = session_day(db)
    if not day:
        return {"day": None, "events": []}
    rule = _rules_names(sch)
    hi = _next_day(day)
    lo = max(day, meta.get("live_from") or "")
    ev = []
    for cid, sym, side, ts, q, px, rp, ch in db.execute("SELECT config_id, symbol, side, fill_ts, qty, price, realized_paise, charges_paise FROM paper_orders "
                                                        "WHERE status IN ('filled','partial') AND fill_ts >= ? AND fill_ts < ? ORDER BY fill_ts DESC LIMIT ?",
                                                        (day, hi, limit)):
        rn, tf = rule(cid)
        ev.append({"ts": ts, "kind": "BUY" if side == "BUY" else "SELL", "symbol": sym,
                   "text": (f"Paper buy {q} @ {px:,.2f} · {rn} {tf}" if side == "BUY" else
                            f"Paper sell {q} @ {px:,.2f} · net {'+' if (rp or 0) >= 0 else '−'}₹{abs((rp or 0) / 100):,.2f} after charges")})
    for ts, sym, why, n in db.execute("SELECT bar_ts, symbol, alloc_reason, COUNT(*) FROM signals WHERE decision='BUY' AND alloc_status != 'accepted' "
                                      "AND bar_ts >= ? AND bar_ts < ? GROUP BY 1, 2 ORDER BY bar_ts DESC LIMIT ?", (lo, hi, limit)):
        cat = reason_category(why)
        if cat in ("warm-up history", "catch-up (late data, not traded)"):
            continue
        ev.append({"ts": ts, "kind": "REFUSED", "symbol": sym, "category": cat,
                   "text": f"{n} buy signal{'s' if n > 1 else ''} refused: {why if why and len(why) < 140 else cat}"})
    try:
        for ts, e, detail in db.execute("SELECT ts, event, detail FROM portfolio_events WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 10", (day, hi)):
            try:
                d = json.loads(detail)
            except ValueError:
                d = {}
            ev.append({"ts": ts, "kind": "GATE", "symbol": None, "text": str(d.get("reason") or e).replace("_", " ")})
    except Exception:       # noqa: BLE001 - older ledgers
        pass
    ev.sort(key=lambda z: z["ts"], reverse=True)
    return {"day": day, "events": ev[:limit]}
