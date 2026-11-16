"""Performance metrics from an equity curve and a list of round-trip trades.

Conventions: returns are simple daily returns of account equity (intraday curves are
sampled at each session's last bar); annualisation uses 252 sessions; Sharpe/Sortino use a
zero risk-free rate unless `rf_annual` is given. Ratios that are undefined for the sample
(e.g. CAGR over a few weeks, Sharpe with zero variance) are returned as None rather than
a misleading number.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def daily_equity(ts: np.ndarray, equity_paise: np.ndarray) -> pd.Series:
    if len(ts) == 0:
        return pd.Series(dtype=float)
    s = pd.Series(np.asarray(equity_paise, dtype=float) / 100.0, index=pd.to_datetime(ts))
    return s.groupby(s.index.normalize()).last()


def _f(x) -> float | None:
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else float(x)


def drawdown_stats(eq: pd.Series) -> dict:
    if len(eq) == 0:
        return {"max_drawdown": 0.0, "max_drawdown_days": 0, "current_drawdown": 0.0}
    peak = eq.cummax()
    dd = eq / peak - 1
    under = dd < 0
    longest = cur = 0
    start = None
    for t, u in under.items():
        if u:
            start = start or t
            cur = (t - start).days
            longest = max(longest, cur)
        else:
            start = None
    return {"max_drawdown": float(dd.min()), "max_drawdown_days": int(longest), "current_drawdown": float(dd.iloc[-1])}


def sharpe(returns: np.ndarray, rf_annual: float = 0.0) -> float | None:
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return None
    ex = r - rf_annual / TRADING_DAYS
    sd = ex.std(ddof=1)
    return float(ex.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 1e-12 else None


def compute_metrics(ts: np.ndarray, equity_paise: np.ndarray, trades: list[dict], *, initial_capital: float,
                    total_charges: float = 0.0, traded_value: float = 0.0, gross_exposure: np.ndarray | None = None,
                    participations: list[float] | None = None, rf_annual: float = 0.0,
                    max_participation: float = 0.10) -> dict:
    eq = daily_equity(ts, equity_paise)
    n_days = len(eq)
    out: dict = {"sessions": n_days, "initial_capital": initial_capital}
    if n_days == 0:
        return out
    final = float(eq.iloc[-1])
    rets = eq.pct_change().dropna().to_numpy()
    first_ret = eq.iloc[0] / initial_capital - 1
    rets_all = np.concatenate([[first_ret], rets]) if n_days else rets
    years = n_days / TRADING_DAYS
    total_return = final / initial_capital - 1
    out.update(net_profit=final - initial_capital, final_equity=final, total_return=total_return,
               years=round(years, 3))
    out["cagr"] = _f((final / initial_capital) ** (1 / years) - 1) if years >= 0.5 and final > 0 else None
    out["volatility"] = _f(rets_all.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(rets_all) > 1 else None
    out["sharpe"] = sharpe(rets_all, rf_annual)
    downside = np.minimum(rets_all - rf_annual / TRADING_DAYS, 0)
    dd_dev = math.sqrt((downside ** 2).mean()) if len(rets_all) else 0.0
    out["sortino"] = _f((rets_all.mean() - rf_annual / TRADING_DAYS) / dd_dev * math.sqrt(TRADING_DAYS)) if dd_dev > 1e-12 else None
    dd = drawdown_stats(pd.concat([pd.Series([initial_capital], index=[eq.index[0] - pd.Timedelta(days=1)]), eq]))
    out.update(dd)
    out["calmar"] = _f(out["cagr"] / abs(dd["max_drawdown"])) if out["cagr"] is not None and dd["max_drawdown"] < 0 else None
    # tail risk
    if len(rets_all) >= 20:
        q = np.quantile(rets_all, 0.05)
        out.update(var_95=float(q), cvar_95=float(rets_all[rets_all <= q].mean()), worst_day=float(rets_all.min()),
                   best_day=float(rets_all.max()), skew=_f(pd.Series(rets_all).skew()),
                   kurtosis=_f(pd.Series(rets_all).kurt()))
    else:
        out.update(var_95=None, cvar_95=None, worst_day=_f(rets_all.min()) if len(rets_all) else None,
                   best_day=_f(rets_all.max()) if len(rets_all) else None, skew=None, kurtosis=None)
    # trades
    net = np.array([t["net"] for t in trades], dtype=float)
    wins, losses = net[net > 0], net[net <= 0]
    out["trades"] = int(len(net))
    out["win_rate"] = float(len(wins) / len(net)) if len(net) else None
    out["avg_win"] = float(wins.mean()) if len(wins) else None
    out["avg_loss"] = float(losses.mean()) if len(losses) else None
    out["profit_factor"] = _f(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None
    out["expectancy"] = float(net.mean()) if len(net) else None
    out["avg_holding_days"] = float(np.mean([t["holding_days"] for t in trades])) if trades else None
    out["gross_trade_pnl"] = float(sum(t["gross"] for t in trades))
    out["total_costs"] = float(total_charges)
    gp = out["gross_trade_pnl"]
    out["costs_pct_of_gross_pnl"] = _f(total_charges / gp) if gp > 0 else None
    out["traded_value"] = float(traded_value)
    avg_eq = float(eq.mean())
    out["turnover_annual"] = _f(traded_value / avg_eq / years) if years > 0 and avg_eq > 0 else None
    if gross_exposure is not None and len(gross_exposure):
        g = np.asarray(gross_exposure, dtype=float)
        e = np.asarray(equity_paise, dtype=float)
        out["exposure_time"] = float((g > 0).mean())
        out["avg_gross_leverage"] = float(np.mean(g / np.maximum(e, 1)))
    # capacity: scale at which the 95th-percentile fill would hit the participation cap
    if participations:
        p95 = float(np.quantile(participations, 0.95))
        out["p95_participation"] = p95
        out["capacity_estimate"] = _f(initial_capital * max_participation / p95) if p95 > 0 else None
        out["capacity_note"] = ("capital at which the 95th-percentile fill reaches the participation cap; "
                                "a rough upper bound, not a guarantee")
    else:
        out["p95_participation"] = None
        out["capacity_estimate"] = None
    return out


def returns_from_equity(ts: np.ndarray, equity_paise: np.ndarray, initial_capital: float) -> pd.Series:
    eq = daily_equity(ts, equity_paise)
    if len(eq) == 0:
        return pd.Series(dtype=float)
    r = eq.pct_change()
    r.iloc[0] = eq.iloc[0] / initial_capital - 1
    return r
