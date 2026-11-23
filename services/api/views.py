"""Aggregate read models for the dashboard (Command Center, platform status strip).

Each section carries its own `provenance` so the UI can label every number: where it comes from, the
period it covers, when it was computed, and whether the underlying data is synthetic, replayed, delayed
or live. Values that are not known are None, never estimated.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

IST = ZoneInfo("Asia/Kolkata")


def market_session(calendar, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST).replace(tzinfo=None)
    session = calendar.is_session(now.date())
    m = now.hour * 60 + now.minute
    if not session:
        state = "closed (holiday or weekend)"
    elif m < 9 * 60:
        state = "pre-open later today"
    elif m < 9 * 60 + 15:
        state = "pre-open"
    elif m < 15 * 60 + 30:
        state = "open"
    else:
        state = "closed for the day"
    return {"open": state == "open", "state": state, "ist_time": now.strftime("%Y-%m-%d %H:%M"),
            "holiday_calendar": "supplied" if getattr(calendar, "holidays", None) else "not supplied (weekends only)"}


def _drawdown(eq: list[float]) -> dict | None:
    if len(eq) < 2:
        return None
    a = np.asarray(eq, dtype=float)
    peak = np.maximum.accumulate(a)
    dd = 1 - a / peak
    return {"max_pct": round(float(dd.max()) * 100, 3), "current_pct": round(float(dd[-1]) * 100, 3)}


def portfolio_section(sch, meta: dict | None = None) -> dict | None:
    if sch is None:
        return None
    x = sch.engine.x
    snap = x.snapshot()
    db = sch.store.db
    with sch.store._lock:
        eq = [r[0] for r in db.execute("SELECT equity_inr FROM cycles WHERE equity_inr IS NOT NULL ORDER BY minute")]
        span = db.execute("SELECT MIN(minute), MAX(minute) FROM cycles WHERE equity_inr IS NOT NULL").fetchone()
        realised = db.execute("SELECT COALESCE(SUM(realized_paise),0), COALESCE(SUM(charges_paise),0) FROM paper_orders WHERE side='SELL'").fetchone()
    r, e = sch.reg, sch.engine
    held = e.pos == 1
    from nseql.screening.registry import FAMILIES
    fam_open = {FAMILIES[f]: int(n) for f, n in zip(*np.unique(r.fam[held], return_counts=True))} if held.any() else {}
    from nseql.screening.metrics import account_figures
    fig = account_figures(sch, meta or {}, datetime.now(IST).strftime("%Y-%m-%d"))
    start = fig["starting_capital"]
    eq_now = snap["equity_inr"]
    curve = eq[-750:]
    step = max(1, len(curve) // 150)
    return {
        "shared_capital": snap["shared_capital"], "starting_capital_inr": start, "equity_inr": eq_now,
        "available_inr": round(snap["cash_inr"] - snap["reserved_inr"], 2), "deployed_inr": snap["gross_exposure_inr"],
        "reserved_inr": snap["reserved_inr"], "utilisation_pct": round(100 * (snap["gross_exposure_inr"] + snap["reserved_inr"]) / eq_now, 2) if eq_now else None,
        "return_pct": round((eq_now / start - 1) * 100, 3) if start else None, "drawdown": _drawdown(eq),
        "pnl_today_inr": fig["pnl_today"], "pnl_since_start_inr": fig["pnl_since_start"], "windows": fig["windows"],
        "realised_after_charges_inr": realised[0] / 100, "charges_inr": realised[1] / 100,
        "open_positions": snap["open_positions"], "top_symbols": snap["top_symbols"][:5], "open_by_family": fam_open,
        "halted_today": snap["halted_today"], "rules": snap["rules"],
        "equity_curve": {"ts": None, "values": curve[::step]},
        "provenance": {"source": "screening forward paper ledger (simulated fills)", "period": list(span) if span else None,
                       "data": sch.provenance()},
    }


def command_center(*, st, sch, reg, brain_status: dict, brain_state: dict, readiness: list[dict], calendar, universe_summary: dict | None,
                   sandbox_status: tuple[bool, str], meta: dict | None = None) -> dict:
    t0 = time.perf_counter()
    now = datetime.now(IST)
    jobs = list(st.jobs.values())
    running = [j for j in jobs if j["status"] == "running"]
    failed = [j for j in jobs if j["status"] == "error"][-5:]
    ds = sch.provenance() if sch is not None else {"kind": "none", "label": "No market data attached", "live": False, "verified": False}
    # ---------------------------------------------------------------- platform
    ready = {m["module"]: m for m in readiness}
    platform = {
        "data_source": ds, "market": market_session(calendar),
        "broker": {"angel_one": ready.get("Angel One adapter"), "live_orders": "disabled" if not st.settings.live_trading_enabled else "flag set (gate still closed)"},
        "workers": {"screening_loop": "running" if (sch is not None and sch._thread is not None and sch._thread.is_alive()) else
                    ("idle (replay finished or not started)" if sch is not None else "not started"),
                    "jobs_running": [{"id": j["id"], "kind": j["kind"], "started": j.get("started")} for j in running],
                    "jobs_failed_recently": [{"id": j["id"], "kind": j["kind"], "error": (j.get("error") or "")[:160]} for j in failed]},
        "research_service": {"available": sandbox_status[0], "detail": sandbox_status[1]},
        "paper_trading": {"sessions": len(st.sessions), "screening_paper_orders": "on" if (sch is not None and not st.kill.engaged) else "off"},
        "risk": {"kill_switch": {"engaged": st.kill.engaged, "reason": st.kill.reason},
                 "daily_loss_halt": (sch.engine.x.halted_day if sch is not None else None),
                 "live_trading_enabled": st.settings.live_trading_enabled},
        "readiness": readiness,
        "provenance": {"source": "runtime state of this API process", "updated": now.isoformat(timespec="seconds")},
    }
    # ---------------------------------------------------------------- strategies
    hyps = reg.hypotheses(100000)
    hs: dict[str, int] = {}
    for h in hyps:
        if h.get("origin") != "system":
            hs[h["status"]] = hs.get(h["status"], 0) + 1
    exps = reg.experiments(20)
    strat = {"screening": None, "research": {"hypotheses_by_status": hs, "backtests": len(reg.runs(limit=100000)),
                                             "experiments": [{"id": e_["id"], "title": e_.get("title"), "status": e_.get("status"),
                                                              "started": e_.get("created_at")} for e_ in exps[:6]]}}
    if sch is not None and universe_summary is not None:
        db = sch.store.db
        with sch.store._lock:
            cyc = db.execute("SELECT latency_ms, evaluated, minute FROM cycles WHERE status='processed' ORDER BY minute DESC LIMIT 375").fetchall()
        lat = [c[0] for c in cyc if c[0] is not None]
        evald = [c[1] for c in cyc if c[1] is not None]
        strat["screening"] = {
            "registered": universe_summary["registered"], "rules": universe_summary["rules"], "families": universe_summary["families"],
            "note": universe_summary["note"], "by_stage": universe_summary["by_stage"], "by_timeframe": universe_summary["by_timeframe"],
            "by_horizon": universe_summary["by_horizon"], "by_asset_class": {"cash equity": universe_summary["registered"]},
            "evaluated_last_cycle": int((sch.engine.last_eval == sch.engine.last_minute).sum()) if sch.engine.last_minute >= 0 else 0,
            "latency_ms": {"p50": float(np.percentile(lat, 50)), "p99": float(np.percentile(lat, 99)), "series": lat[::-1][-120:]} if lat else None,
            "throughput_configs_per_s": round(float(np.sum(evald) / (np.sum(lat) / 1000)), 0) if lat and np.sum(lat) > 0 else None,
            "funnel": universe_summary["funnel"],
            "provenance": {"source": "screening registry and cycle log", "period": [cyc[-1][2], cyc[0][2]] if cyc else None, "data": ds},
        }
    # ---------------------------------------------------------------- AI
    lessons = reg.lessons(12)
    discoveries = [h for h in hyps if h["status"] in ("paper_candidate", "promoted_to_paper_candidate")][:6]
    rejected = [h for h in hyps if str(h["status"]).startswith("rejected")][:6]
    versions = list((brain_state or {}).get("versions", {}).values()) if isinstance((brain_state or {}).get("versions"), dict) else (brain_state or {}).get("versions", [])
    ai = {
        "active_experiments": [{"id": j["id"], "started": j.get("started"), "progress": len(j.get("progress") or [])} for j in running if j["kind"] == "research"],
        "discoveries": [{"id": h["id"], "family": h.get("family"), "status": h["status"], "reason": h.get("status_reason")} for h in discoveries],
        "failed_hypotheses": [{"id": h["id"], "family": h.get("family"), "status": h["status"], "reason": h.get("status_reason")} for h in rejected],
        "lessons": [{"family": l["family"], "outcome": l["outcome"], "text": l["text"], "next_step": l.get("next_step"), "at": l.get("created_at")} for l in lessons[:6]],
        "memory": {"episodes": (brain_status or {}).get("memory_episodes")},
        "policy_versions": [{"id": v["id"], "status": v["status"], "origin": v.get("origin"), "reason": v.get("reason"),
                             "benchmark": bool(v.get("benchmark")), "forward_epochs": len(v.get("forward") or [])} for v in versions][-8:],
        "champion": (brain_state or {}).get("champion"),
        "learning_verdict": (brain_status or {}).get("verdict"), "kpis": (brain_status or {}).get("kpis"),
        "provenance": {"source": "research registry and Learning Brain memory", "data": "research runs on the datasets recorded with each run"},
    }
    return {"generated_at": now.isoformat(timespec="seconds"), "platform": platform, "strategies": strat, "ai": ai,
            "portfolio": portfolio_section(sch, meta), "compute_ms": round((time.perf_counter() - t0) * 1000, 1)}
