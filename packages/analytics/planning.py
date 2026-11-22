"""Investment planning: SIP, step-up SIP, lump sum, SWP, STP, goal planning, XIRR.

Two kinds of calculation, never mixed:

* PROJECTIONS (`project_*`, `goal_*`): forward arithmetic at a return the USER assumes. They are
  illustrations of compounding, not forecasts; every result carries the assumptions it used.
* HISTORICAL simulations on NAV series (`*_on_nav`): what an investor would have received, using the
  fund's published daily NAVs. Accounting rules (mutual funds in India):
    - a purchase on a non-business day is allotted at the next available NAV;
    - stamp duty of 0.005% is deducted from purchase amounts made on or after 2020-07-01;
    - units are rounded down to 3 decimals (registrar practice);
    - a redemption is paid at the NAV of its date (or the next available), less any exit load;
    - taxes (capital gains, TDS) are NOT modelled.
  XIRR is computed on the actual dated cash flows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

STAMP_DUTY = 0.00005
STAMP_DUTY_FROM = date(2020, 7, 1)


# ------------------------------------------------------------------------------------------ XIRR
def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annualised internal rate of return of dated cash flows (investments negative, receipts positive).
    None when it is undefined (all flows the same sign) or does not converge."""
    flows = [(d, float(a)) for d, a in flows if a]
    if not flows or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)
    t = np.array([(d - d0).days / 365.0 for d, _ in flows])
    a = np.array([x for _, x in flows])
    f = lambda r: float(np.sum(a / (1 + r) ** t))
    lo, hi = -0.9999, 10.0
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):                                     # bisection: robust for any sign pattern with one root in range
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-9:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def monthly_rate(annual: float) -> float:
    return (1 + annual) ** (1 / 12) - 1


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError(d)


# ------------------------------------------------------------------------------------------ projections
@dataclass(frozen=True)
class Assumptions:
    annual_return: float
    inflation: float = 0.0

    def to_dict(self) -> dict:
        return {"annual_return": self.annual_return, "inflation": self.inflation,
                "nature": "user-assumed constant return; an illustration of compounding, not a forecast"}


def _sip_fv(monthly: float, months: int, annual_return: float, step_up: float) -> float:
    r = monthly_rate(annual_return)
    value, inst = 0.0, monthly
    for k in range(months):
        if k and k % 12 == 0:
            inst *= 1 + step_up
        value = (value + inst) * (1 + r)
    return value


def project_sip(monthly: float, months: int, annual_return: float, *, step_up: float = 0.0, start: date | None = None) -> dict:
    """Instalment at the start of each month; `step_up` raises the instalment once a year (e.g. 0.10)."""
    if monthly <= 0 or months <= 0:
        raise ValueError("monthly amount and months must be positive")
    r = monthly_rate(annual_return)
    start = start or date.today().replace(day=1)
    value = invested = 0.0
    inst = monthly
    rows, flows = [], []
    for k in range(months):
        if k and k % 12 == 0:
            inst *= 1 + step_up
        invested += inst
        value = (value + inst) * (1 + r)
        flows.append((add_months(start, k), -inst))
        if (k + 1) % 12 == 0 or k == months - 1:
            rows.append({"month": k + 1, "invested": round(invested, 2), "value": round(value, 2)})
    end = add_months(start, months)
    return {"kind": "sip", "monthly": monthly, "step_up": step_up, "months": months, "invested": round(invested, 2), "value": round(value, 2),
            "gain": round(value - invested, 2), "xirr": xirr(flows + [(end, value)]), "schedule": rows,
            "assumptions": Assumptions(annual_return).to_dict()}


def project_lumpsum(amount: float, years: float, annual_return: float) -> dict:
    value = amount * (1 + annual_return) ** years
    return {"kind": "lumpsum", "invested": amount, "value": round(value, 2), "gain": round(value - amount, 2), "years": years,
            "assumptions": Assumptions(annual_return).to_dict()}


def project_swp(corpus: float, monthly: float, annual_return: float, *, months: int = 600, step_up: float = 0.0) -> dict:
    """Withdrawal at the start of each month; growth on the remainder. Stops when the corpus runs out."""
    r = monthly_rate(annual_return)
    bal, w, paid, rows = corpus, monthly, 0.0, []
    depleted = None
    for k in range(months):
        if k and k % 12 == 0:
            w *= 1 + step_up
        take = min(w, bal)
        bal -= take
        paid += take
        if bal <= 1e-9:
            depleted = k + 1
            bal = 0.0
            rows.append({"month": k + 1, "withdrawn": round(paid, 2), "balance": 0.0})
            break
        bal *= 1 + r
        if (k + 1) % 12 == 0:
            rows.append({"month": k + 1, "withdrawn": round(paid, 2), "balance": round(bal, 2)})
    return {"kind": "swp", "corpus": corpus, "monthly": monthly, "step_up": step_up, "withdrawn": round(paid, 2), "balance": round(bal, 2),
            "depleted_after_months": depleted, "lasts": "beyond the horizon" if depleted is None else f"{depleted} months",
            "schedule": rows, "assumptions": Assumptions(annual_return).to_dict()}


def project_stp(source_corpus: float, monthly_transfer: float, source_return: float, target_return: float, *, months: int | None = None) -> dict:
    """Systematic transfer from a source fund (e.g. liquid) into a target fund (e.g. equity), start of month."""
    rs, rt = monthly_rate(source_return), monthly_rate(target_return)
    src, tgt, moved, k, rows = source_corpus, 0.0, 0.0, 0, []
    limit = months or 1200
    while src > 1e-9 and k < limit:
        x = min(monthly_transfer, src)
        src -= x
        tgt += x
        moved += x
        src *= 1 + rs
        tgt *= 1 + rt
        k += 1
        if k % 12 == 0 or src <= 1e-9:
            rows.append({"month": k, "source": round(src, 2), "target": round(tgt, 2)})
    return {"kind": "stp", "months": k, "transferred": round(moved, 2), "source_balance": round(src, 2), "target_value": round(tgt, 2),
            "total": round(src + tgt, 2), "schedule": rows,
            "assumptions": {"source_return": source_return, "target_return": target_return,
                            "nature": "user-assumed constant returns; an illustration, not a forecast"}}


def goal_plan(target_today: float, years: float, annual_return: float, inflation: float, *, existing: float = 0.0, step_up: float = 0.0) -> dict:
    """Monthly SIP needed (with optional yearly step-up) to reach a goal stated in today's money."""
    if years <= 0:
        raise ValueError("years must be positive")
    months = round(years * 12)
    target = target_today * (1 + inflation) ** years
    from_existing = existing * (1 + annual_return) ** years
    gap = max(0.0, target - from_existing)
    if gap == 0:
        need = 0.0
    else:
        unit = _sip_fv(1.0, months, annual_return, step_up)                     # value of Rs 1/month (unrounded): linear in the instalment
        need = gap / unit
    return {"kind": "goal", "target_today": target_today, "target_future": round(target, 2), "years": years, "months": months,
            "existing_grows_to": round(from_existing, 2), "monthly_sip_needed": round(need, 2), "step_up": step_up,
            "assumptions": Assumptions(annual_return, inflation).to_dict()}


# ------------------------------------------------------------------------------------------ historical (NAV series)
def _nav_on_or_after(dates: list[date], navs: list[float], d: date) -> tuple[date, float] | None:
    import bisect
    i = bisect.bisect_left(dates, d)
    return (dates[i], navs[i]) if i < len(dates) else None


def _units(amount: float, nav: float, d: date) -> tuple[float, float]:
    stamp = amount * STAMP_DUTY if d >= STAMP_DUTY_FROM else 0.0
    return math.floor((amount - stamp) / nav * 1000) / 1000, stamp


def sip_on_nav(dates: list[date], navs: list[float], monthly: float, start: date, end: date, *, day: int = 1,
               step_up: float = 0.0, exit_load: float = 0.0) -> dict:
    """A monthly SIP on day `day` of each month from `start` to `end`, valued (redeemed) on the last NAV on or before `end`."""
    if len(dates) != len(navs) or not dates:
        raise ValueError("need a NAV series")
    pairs = sorted(zip(dates, navs))
    dates, navs = [p[0] for p in pairs], [p[1] for p in pairs]
    units = invested = stamp = 0.0
    flows, alloc = [], []
    k, inst = 0, monthly
    d = date(start.year, start.month, min(day, 28))
    if d < start:
        d = add_months(d, 1)
    while d <= end:
        if k and k % 12 == 0:
            inst *= 1 + step_up
        hit = _nav_on_or_after(dates, navs, d)
        if hit is None or hit[0] > end:
            break
        u, s = _units(inst, hit[1], hit[0])
        units += u
        invested += inst
        stamp += s
        flows.append((hit[0], -inst))
        alloc.append({"date": hit[0].isoformat(), "amount": round(inst, 2), "nav": hit[1], "units": u})
        k += 1
        d = add_months(date(start.year, start.month, min(day, 28)), k)
    import bisect
    j = bisect.bisect_right(dates, end) - 1
    if j < 0 or not alloc:
        raise ValueError("no NAV inside the period")
    vd, vnav = dates[j], navs[j]
    value = units * vnav * (1 - exit_load)
    return {"kind": "sip_historical", "instalments": len(alloc), "invested": round(invested, 2), "units": round(units, 3),
            "valuation_date": vd.isoformat(), "valuation_nav": vnav, "value": round(value, 2), "gain": round(value - invested, 2),
            "stamp_duty": round(stamp, 2), "xirr": xirr(flows + [(vd, value)]), "allotments": alloc,
            "rules": "next-available NAV for non-business days; 0.005% stamp duty from 2020-07-01; units floored to 3 decimals; taxes not modelled"}


def lumpsum_on_nav(dates: list[date], navs: list[float], amount: float, start: date, end: date, *, exit_load: float = 0.0) -> dict:
    pairs = sorted(zip(dates, navs))
    dates, navs = [p[0] for p in pairs], [p[1] for p in pairs]
    hit = _nav_on_or_after(dates, navs, start)
    import bisect
    j = bisect.bisect_right(dates, end) - 1
    if hit is None or j < 0 or dates[j] < hit[0]:
        raise ValueError("no NAV inside the period")
    u, s = _units(amount, hit[1], hit[0])
    value = u * navs[j] * (1 - exit_load)
    return {"kind": "lumpsum_historical", "invested": amount, "purchase_date": hit[0].isoformat(), "purchase_nav": hit[1], "units": u,
            "valuation_date": dates[j].isoformat(), "value": round(value, 2), "gain": round(value - amount, 2), "stamp_duty": round(s, 2),
            "xirr": xirr([(hit[0], -amount), (dates[j], value)])}


def nav_analytics(dates: list[date], navs: list[float]) -> dict:
    """Point-to-point CAGR (1/3/5 years, when the series covers them), volatility, max drawdown, rolling 1-year returns."""
    pairs = sorted(zip(dates, navs))
    d = [p[0] for p in pairs]
    v = np.array([p[1] for p in pairs], dtype=float)
    if len(v) < 2:
        return {"points": len(v)}
    import bisect
    last = d[-1]
    out = {"points": len(v), "first": d[0].isoformat(), "last": last.isoformat(), "last_nav": float(v[-1]), "cagr": {}}
    for yrs in (1, 3, 5, 10):
        try:
            t0 = last.replace(year=last.year - yrs)
        except ValueError:
            t0 = last - timedelta(days=365 * yrs)
        i = bisect.bisect_right(d, t0) - 1
        out["cagr"][f"{yrs}y"] = None if i < 0 else round(float((v[-1] / v[i]) ** (1 / yrs) - 1), 6)
    lr = np.diff(np.log(v))
    span_years = max((last - d[0]).days / 365.25, 1e-9)
    per_year = len(lr) / span_years
    out["volatility_annual"] = round(float(lr.std(ddof=1) * math.sqrt(per_year)), 6) if len(lr) > 2 else None
    peak = np.maximum.accumulate(v)
    out["max_drawdown"] = round(float((1 - v / peak).max()), 6)
    roll = []
    for k in range(len(d)):
        try:
            t1 = d[k].replace(year=d[k].year + 1)
        except ValueError:
            t1 = d[k] + timedelta(days=365)
        j = bisect.bisect_left(d, t1)
        if j < len(d):
            roll.append(float(v[j] / v[k] - 1))
    if roll:
        r = np.array(roll)
        out["rolling_1y"] = {"windows": len(r), "min": round(float(r.min()), 6), "median": round(float(np.median(r)), 6),
                             "max": round(float(r.max()), 6), "share_negative": round(float((r < 0).mean()), 4)}
    return out
