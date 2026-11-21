"""The research team. Deterministic implementations; the inventor optionally consults an LLM."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from nseql.agents import llm as llm_mod
from nseql.contracts.artifacts import (AuditReport, CodeArtifact, Decision, Evidence, Finding, MarketBrief,
                                       ReadinessReport, RiskReport)
from nseql.contracts.spec import Feature, Sizing, StrategySpec
from nseql.data.store import Dataset
from nseql.research import sandbox
from nseql.research.registry import Registry
from nseql.strategies.codegen import generate_source

FAMILIES = ("trend", "momentum", "mean_reversion", "breakout", "volatility")


# =============================================================================== Market Intelligence
class MarketIntelligenceAgent:
    name = "market_intelligence"

    def brief(self, ds: Dataset, start: str, end: str, interval: str = "1d") -> MarketBrief:
        """Descriptive statistics of the development window only (never validation or sealed)."""
        closes, values, sectors = {}, {}, {}
        for s in ds.symbols(interval):
            inst = ds.instruments[s]
            if not inst.is_cash:
                continue
            df = ds.bars(s, interval, start, end)
            if len(df) < 150:
                continue
            g = np.ones(len(df))
            for ca in ds.corporate_actions():
                if ca.symbol == s and ca.kind in ("split", "bonus"):
                    g[df["ts"].dt.date.to_numpy() < ca.ex_date] *= 1 / ca.ratio
            closes[s] = pd.Series(df["close"].to_numpy() * g, index=df["ts"].dt.normalize())
            values[s] = float((df["close"] * df["volume"]).median())
            sectors[s] = inst.sector or "Unknown"
        if len(closes) < 3:
            return MarketBrief(dataset_id=ds.id, synthetic=ds.synthetic, window=(start, end), symbols=len(closes),
                               sessions=0, notes=["fewer than three usable symbols"],
                               family_priors={f: 1.0 for f in FAMILIES})
        px = pd.DataFrame(closes).sort_index()
        r = px.pct_change()
        ic = lambda a, b: float(a.corrwith(b, axis=1, method="spearman").mean())
        mom = ic(px.pct_change(120), px.pct_change(21).shift(-21))
        rev = ic(px.pct_change(3), px.pct_change(3).shift(-3))
        ac1 = float(np.nanmean([r[c].autocorr(1) for c in r]))
        corr = r.corr().to_numpy()
        avg_corr = float((corr.sum() - len(corr)) / (len(corr) * (len(corr) - 1)))
        vol = r.std() * math.sqrt(252)
        mkt = r.mean(axis=1)
        recent = float(mkt.tail(20).std() / mkt.rolling(20).std().median())
        sec = {}
        for s, c in closes.items():
            sec.setdefault(sectors[s], []).append(float(c.iloc[-1] / c.iloc[0] - 1))
        clip = lambda x: float(min(max(x, 0.2), 3.0))
        pri = {"trend": clip(1 + 6 * mom), "momentum": clip(1 + 8 * mom), "mean_reversion": clip(1 - 10 * rev),
               "breakout": clip(1 + 4 * mom + 3 * ac1), "volatility": clip(recent)}
        notes = [f"momentum rank-IC {mom:+.3f}; short-horizon reversal rank-IC {rev:+.3f}; lag-1 autocorrelation {ac1:+.3f}",
                 "statistics are in-sample descriptions of the development window and are themselves noisy"]
        return MarketBrief(dataset_id=ds.id, synthetic=ds.synthetic, window=(start, end), symbols=len(closes),
                           sessions=len(px), median_annual_vol=float(vol.median()), avg_pairwise_correlation=avg_corr,
                           momentum_score=mom, reversal_score=rev, lag1_autocorrelation=ac1, recent_vol_vs_median=recent,
                           median_daily_traded_value=float(np.median(list(values.values()))),
                           sector_returns={k: float(np.mean(v)) for k, v in sorted(sec.items())}, family_priors=pri, notes=notes)


# =============================================================================== Strategy Inventor
def _spec(sid, family, hyp, fals, params, grid, features, entry, exit_, *, max_hold=None, stop=None, slots=5,
          regimes=(), failures=(), origin="grammar", parent=None, rationale="") -> StrategySpec:
    return StrategySpec(id=sid, family=family, hypothesis=hyp, falsification=fals, params=params, param_grid=grid,
                        features=[Feature(**f) for f in features], entry=entry, exit=exit_, max_hold=max_hold,
                        stop_loss_pct=stop, sizing=Sizing(max_positions=slots), regime_assumptions=list(regimes),
                        expected_failure_modes=list(failures), origin=origin, parent_id=parent, rationale=rationale)


def _templates(rng: np.random.Generator) -> dict:
    """Hypothesis grammar: each family has structurally different rule templates."""
    pick = lambda xs: xs[int(rng.integers(len(xs)))]
    F = lambda name, fn, n, source="close": {"name": name, "fn": fn, "n": n, "source": source}

    def trend_cross(sid):
        fast, slow = pick([10, 20, 30]), pick([80, 120, 160])
        return _spec(sid, "trend", "When a fast moving average is above a slow one the stock is in an "
                     "established uptrend that persists for several weeks.",
                     "Refuted if net Sharpe on validation data is not above the random-entry baseline, or the edge "
                     "vanishes when costs are doubled.",
                     {"fast": fast, "slow": slow}, {"fast": [10, 20, 30], "slow": [80, 120, 160]},
                     [F("ma_fast", "sma", "fast"), F("ma_slow", "sma", "slow")], "ma_fast > ma_slow", "ma_fast < ma_slow",
                     regimes=["trending markets"], failures=["whipsaw in range-bound markets", "late entries after gaps"])

    def trend_pullback(sid):
        return _spec(sid, "trend", "In an uptrend (price above its long average) a short pullback, shown by a low "
                     "short-window RSI, is followed by resumption of the trend.",
                     "Refuted if average net trade P&L on validation data is not positive or win rate is below 50%.",
                     {"trend_n": pick([100, 150]), "rsi_buy": pick([25, 35]), "rsi_sell": 65},
                     {"trend_n": [100, 150, 200], "rsi_buy": [20, 25, 35]},
                     [F("trend_ma", "sma", "trend_n"), F("r", "rsi", 5)], "close > trend_ma and r < rsi_buy",
                     "r > rsi_sell or close < trend_ma", max_hold=15, regimes=["persistent uptrends"],
                     failures=["trend reversals look like pullbacks at first"])

    def momentum_roc(sid):
        n = pick([60, 90, 120])
        return _spec(sid, "momentum", "Stocks with a strong trailing 3-6 month return and price above the 50-day average keep "
                     "outperforming over the next month.",
                     "Refuted if the strategy does not beat random entry with the same holding period on validation data.",
                     {"roc_n": n, "roc_min": pick([0.10, 0.15]), "exit_roc": 0.0}, {"roc_n": [60, 90, 120], "roc_min": [0.05, 0.10, 0.15]},
                     [F("mom", "roc", "roc_n"), F("ma50", "sma", 50)], "mom > roc_min and close > ma50",
                     "mom < exit_roc or close < ma50", max_hold=40, regimes=["momentum regimes"],
                     failures=["momentum crashes after sharp market reversals"])

    def meanrev_z(sid):
        n = pick([10, 20])
        return _spec(sid, "mean_reversion", "A close more than z standard deviations below its short-term mean is an "
                     "overreaction that reverts within days.",
                     "Refuted if expectancy per trade is not positive after doubling transaction costs.",
                     {"z_n": n, "z_in": pick([-2.0, -1.5]), "z_out": 0.0}, {"z_n": [10, 20, 30], "z_in": [-2.5, -2.0, -1.5]},
                     [F("z", "zscore", "z_n")], "z < z_in", "z > z_out", max_hold=10,
                     regimes=["range-bound, liquid names"], failures=["catching falling knives in downtrends", "costs: high turnover"])

    def meanrev_rsi(sid):
        return _spec(sid, "mean_reversion", "Very low 3-day RSI readings in stocks above their 150-day average mark "
                     "short-term exhaustion that reverses within a week.",
                     "Refuted if win rate on validation data is below 55% or profit factor is below 1.",
                     {"rsi_in": pick([10, 15, 20]), "rsi_out": pick([55, 65])}, {"rsi_in": [10, 15, 20], "rsi_out": [50, 55, 65]},
                     [F("r", "rsi", 3), F("ma", "sma", 150)], "r < rsi_in and close > ma", "r > rsi_out", max_hold=8,
                     regimes=["uptrends with shallow dips"], failures=["regime shifts where dips keep dipping"])

    def breakout_hh(sid):
        n = pick([20, 40, 60])
        return _spec(sid, "breakout", "A close above the highest high of the previous one to three months signals the start of a sustained advance.",
                     "Refuted if breakout entries do not beat random entries with the same average holding period.",
                     {"n_in": n, "n_out": pick([10, 20])}, {"n_in": [20, 40, 60], "n_out": [10, 20]},
                     [F("hh", "highest", "n_in", "high"), F("ll", "lowest", "n_out", "low")], "close > prev(hh)",
                     "close < prev(ll)", stop=0.10, regimes=["trending, expanding-volatility markets"],
                     failures=["false breakouts", "gap fills after the breakout day"])

    def vol_squeeze(sid):
        return _spec(sid, "volatility", "A breakout that follows a period of unusually low realised volatility "
                     "(a squeeze) travels further than an ordinary breakout.",
                     "Refuted if the low-volatility filter does not improve validation Sharpe over the unfiltered breakout.",
                     {"vol_cap": pick([0.18, 0.22, 0.28]), "n_in": 20}, {"vol_cap": [0.18, 0.22, 0.28], "n_in": [20, 40]},
                     [F("rv", "realized_vol", 20), F("hh", "highest", "n_in", "high"), F("ma", "sma", 20)],
                     "prev(rv) < vol_cap and close > prev(hh)", "close < ma", max_hold=30,
                     regimes=["volatility clustering"], failures=["low volatility persisting without a move"])

    def vol_calm_trend(sid):
        return _spec(sid, "volatility", "Uptrends accompanied by falling realised volatility are more persistent than "
                     "uptrends with rising volatility.",
                     "Refuted if drawdown on validation data is not smaller than the plain trend rule's drawdown.",
                     {"vol_cap": pick([0.20, 0.25, 0.30]), "ma_n": 50}, {"vol_cap": [0.20, 0.25, 0.30], "ma_n": [50, 100]},
                     [F("rv", "realized_vol", 20), F("ma", "sma", "ma_n")], "close > ma and rv < vol_cap",
                     "close < ma or rv > vol_cap * 1.5", regimes=["calm bull phases"], failures=["volatility spikes exit late"])

    return {"trend": [trend_cross, trend_pullback], "momentum": [momentum_roc], "mean_reversion": [meanrev_z, meanrev_rsi],
            "breakout": [breakout_hh], "volatility": [vol_squeeze, vol_calm_trend]}


def mutate(parent: StrategySpec, directive: str, sid: str) -> StrategySpec | None:
    """Structural change to a parent hypothesis in response to a diagnosed failure. This is a new
    hypothesis with its own lineage, not a parameter re-tune."""
    d = parent.model_dump()
    d.update(id=sid, parent_id=parent.id, origin="mutation:" + directive, version=1)
    names = {f["name"] for f in d["features"]}
    if directive == "add_trend_filter" and "regime_ma" not in names:
        d["features"].append({"name": "regime_ma", "fn": "sma", "source": "close", "n": 200})
        d["entry"] = f"({d['entry']}) and close > regime_ma"
        d["hypothesis"] += " The effect is conditional on the stock being above its 200-day average."
        d["rationale"] = "parent lost money against the trend; test whether the effect is conditional on a long-term uptrend"
    elif directive == "reduce_turnover":
        d["max_hold"] = int((d.get("max_hold") or 20) * 2)
        d["sizing"]["max_positions"] = max(d["sizing"]["max_positions"] - 2, 2)
        d["hypothesis"] += " Holding winners longer lets the edge exceed transaction costs."
        d["rationale"] = "parent's gross edge was consumed by costs; test a lower-turnover expression of the same idea"
    elif directive == "add_stop" and not d.get("stop_loss_pct"):
        d["stop_loss_pct"] = 0.08
        d["hypothesis"] += " An 8% protective stop truncates the left tail without removing the edge."
        d["rationale"] = "parent's losses were concentrated in a few large losers"
    elif directive == "add_volatility_filter" and "regime_rv" not in names:
        d["features"].append({"name": "regime_rv", "fn": "realized_vol", "source": "close", "n": 20})
        d["params"]["rv_cap"] = 0.35
        d["entry"] = f"({d['entry']}) and regime_rv < rv_cap"
        d["hypothesis"] += " The effect holds only outside high-volatility regimes."
        d["rationale"] = "parent failed in the high-volatility regime"
    else:
        return None
    try:
        return StrategySpec(**d)
    except ValueError:
        return None


class StrategyInventor:
    name = "strategy_inventor"

    def __init__(self, seed: int = 0, llm: llm_mod.LLMClient | None = None):
        self.rng = np.random.default_rng(seed)
        self.llm = llm
        self.n = 0
        self.log: list[str] = []

    def _id(self, tag: str) -> str:
        self.n += 1
        return f"h{pd.Timestamp.now():%m%d%H%M%S}{self.n:02d}{tag}"

    def propose(self, family: str, brief: MarketBrief, memory: dict, seen: set[str],
                mutation: tuple[StrategySpec, str] | None = None) -> StrategySpec | None:
        if mutation:
            child = mutate(mutation[0], mutation[1], self._id("m"))
            if child and child.structural_hash() not in seen:
                return child
        if self.llm is not None:
            spec = self._from_llm(family, brief, memory)
            if spec and spec.structural_hash() not in seen:
                return spec
        makers = _templates(self.rng).get(family) or []
        for _ in range(12):
            if not makers:
                break
            spec = makers[int(self.rng.integers(len(makers)))](self._id(family[:2]))
            if spec.structural_hash() not in seen:
                spec.rationale = spec.rationale or (f"family '{family}' selected by the research director; "
                                                    f"development-sample prior {brief.family_priors.get(family, 1.0):.2f}")
                return spec
            makers = _templates(self.rng)[family]
        return None

    def _from_llm(self, family: str, brief: MarketBrief, memory: dict) -> StrategySpec | None:
        import json
        prompt = (f"Preferred family for this experiment: {family}\n\nDevelopment-sample market brief:\n"
                  f"{brief.model_dump_json(indent=1)}\n\nResearch memory (lessons and prior hypotheses; sealed results are "
                  f"withheld from you by design):\n{json.dumps(memory, default=str)[:20000]}")
        try:
            d = self.llm.json(llm_mod.INVENTOR_SYSTEM, prompt, llm_mod.SPEC_DRAFT_SCHEMA)
            params = {p["name"]: p["value"] for p in d["params"]}
            feats = [{"name": f["name"], "fn": f["fn"], "source": f["source"],
                      "n": int(f["window"]) if str(f["window"]).lstrip("-").isdigit() else f["window"]} for f in d["features"]]
            return _spec(self._id("ai"), d["family"], d["hypothesis"], d["falsification"], params,
                         {p["name"]: p["grid"][:3] for p in d["params"] if p["grid"]}, feats, d["entry"], d["exit"],
                         max_hold=d["max_hold"] or None, stop=d["stop_loss_pct"] or None, slots=min(max(d["max_positions"], 1), 20),
                         regimes=d["regime_assumptions"], failures=d["expected_failure_modes"], origin=f"llm:{self.llm.name}",
                         rationale=d["rationale"])
        except (llm_mod.LLMUnavailable, ValueError, KeyError, TypeError) as e:
            self.log.append(f"LLM proposal unusable ({type(e).__name__}: {str(e)[:200]}); falling back to grammar")
            return None


# =============================================================================== Strategy Developer
class StrategyDeveloper:
    name = "strategy_developer"

    def build(self, spec: StrategySpec, registry: Registry) -> tuple[CodeArtifact, str]:
        source = generate_source(spec)
        rep = sandbox.validate_source(source)
        code_id = registry.add_code(spec.id, source, rep.to_dict()) if rep.ok else None
        return CodeArtifact(hypothesis_id=spec.id, code_id=code_id, sha256=rep.sha256, lines=source.count("\n") + 1,
                            validation_ok=rep.ok, violations=rep.violations), source


# =============================================================================== Skeptical Quant Auditor
@dataclass
class AuditPolicy:
    min_trades_dev: int = 25
    min_trades_validation: int = 6
    min_dev_sharpe: float = 0.3
    min_validation_sharpe: float = 0.2
    max_validation_decay: float = 0.75        # validation Sharpe must keep >= 25% of dev Sharpe
    min_stability_share: float = 0.6
    max_bootstrap_p: float = 0.15
    min_deflated_sharpe: float = 0.5
    max_plausible_sharpe: float = 4.0
    max_top3_profit_share: float = 0.7
    cost_multiplier_must_survive: float = 2.0


class SkepticalQuantAuditor:
    """Independent critic. It did not write the strategy and gets no say in what is proposed next.
    Its job is to find a reason to reject."""
    name = "quant_auditor"

    def __init__(self, policy: AuditPolicy | None = None):
        self.policy = policy or AuditPolicy()

    def review(self, ev: Evidence, synthetic: bool) -> AuditReport:
        p = self.policy
        f: list[Finding] = []
        add = lambda code, sev, msg: f.append(Finding(code=code, severity=sev, message=msg))
        for e in ev.errors:
            add("RUNTIME_ERROR", "fail", e)
        la = ev.lookahead
        if not la:
            add("LOOKAHEAD_UNTESTED", "fail", "the future-perturbation audit did not run")
        elif not la.get("passed"):
            add("LOOKAHEAD", "fail", "orders placed before the cut changed when FUTURE prices were scrambled: "
                                     "the strategy's decisions depend on data it could not have had")
        elif la.get("orders_checked", 0) == 0:
            add("LOOKAHEAD_VACUOUS", "warn", "the look-ahead audit compared zero orders")
        dm = ev.dev.get("metrics", {})
        if dm:
            sh = dm.get("sharpe")
            if dm.get("trades", 0) < p.min_trades_dev:
                add("TOO_FEW_TRADES", "fail", f"{dm.get('trades', 0)} development trades (< {p.min_trades_dev}): no statistical evidence")
            if sh is None or sh < p.min_dev_sharpe:
                add("NO_DEV_EDGE", "fail", f"development Sharpe {sh} is below {p.min_dev_sharpe}")
            elif sh > p.max_plausible_sharpe:
                add("IMPLAUSIBLE", "fail", f"development Sharpe {sh:.2f} is implausibly high; treat as a bug until explained")
            if (dm.get("win_rate") or 0) > 0.9 and dm.get("trades", 0) >= 20:
                add("IMPLAUSIBLE", "fail", f"win rate {dm['win_rate']:.0%} over {dm['trades']} trades is implausible")
        elif not ev.errors:
            add("NO_DEV_RESULT", "fail", "no development backtest result")
        rows = {r["multiplier"]: r for r in ev.cost_sensitivity.get("rows", []) if "error" not in r}
        hi = rows.get(p.cost_multiplier_must_survive)
        if hi is not None and (hi["total_return"] or 0) <= 0:
            add("COST_FRAGILE", "fail", f"unprofitable when costs and slippage are multiplied by {p.cost_multiplier_must_survive:g}")
        z = rows.get(0.0)
        if z and dm and z["total_return"] and dm.get("total_return") is not None and z["total_return"] > 0:
            drag = 1 - dm["total_return"] / z["total_return"]
            if drag > 0.5:
                add("COST_HEAVY", "warn", f"costs consume {drag:.0%} of the frictionless return")
        st = ev.stability
        if st.get("neighbours") and st.get("share_positive") is not None and st["share_positive"] < p.min_stability_share:
            add("PARAM_UNSTABLE", "fail", f"only {st['share_positive']:.0%} of neighbouring parameter sets are profitable")
        lat = ev.stress.get("one_bar_latency", {})
        if lat.get("total_return") is not None and lat["total_return"] <= 0:
            add("LATENCY_FRAGILE", "warn", "unprofitable with one extra bar of execution delay")
        b = ev.baselines
        if b:
            s, rnd, bh = (b.get(k, {}).get("sharpe") for k in ("strategy", "random_entry", "buy_and_hold"))
            if s is not None and rnd is not None and s <= rnd:
                add("NOT_BETTER_THAN_RANDOM", "fail", f"Sharpe {s:.2f} does not beat random entry ({rnd:.2f}) on the same data")
            if s is not None and bh is not None and s < bh:
                add("BELOW_BUY_AND_HOLD", "warn", f"Sharpe {s:.2f} is below equal-weight buy-and-hold ({bh:.2f})")
        if ev.stage != "dev":
            v = ev.validation
            vs = v.get("sharpe")
            if v.get("trades", 0) < p.min_trades_validation:
                add("TOO_FEW_VALIDATION_TRADES", "fail", f"{v.get('trades', 0)} trades in the validation window")
            if vs is None or vs < p.min_validation_sharpe:
                add("VALIDATION_FAILED", "fail", f"validation Sharpe {vs} is below {p.min_validation_sharpe}")
            elif dm.get("sharpe") and vs < dm["sharpe"] * (1 - p.max_validation_decay):
                add("VALIDATION_DECAY", "fail", f"Sharpe fell from {dm['sharpe']:.2f} (development) to {vs:.2f} (validation)")
            bp = ev.bootstrap.get("p_value")
            if bp is not None and bp > p.max_bootstrap_p:
                add("INSIGNIFICANT", "fail", f"block-bootstrap p-value {bp:.2f} for Sharpe > 0 exceeds {p.max_bootstrap_p}")
            dsr = ev.deflated_sharpe.get("dsr")
            if dsr is not None and dsr < p.min_deflated_sharpe:
                add("DSR_LOW", "fail", f"deflated Sharpe {dsr:.2f} after {ev.deflated_sharpe.get('n_trials')} trials: "
                                       "indistinguishable from the best of many random tries")
            wf = ev.walk_forward.get("oos", {})
            if wf and wf.get("sharpe") is not None and wf["sharpe"] <= 0:
                add("WALK_FORWARD_NEGATIVE", "fail", f"walk-forward out-of-sample Sharpe {wf['sharpe']:.2f}")
            hv = ev.regimes.get("high_vol", {})
            if hv.get("total_return") is not None and hv["total_return"] < -0.05:
                add("REGIME_DEPENDENT", "warn", f"loses {hv['total_return']:.1%} in the high-volatility regime")
        pnl = sorted((v for v in ev.pnl_by_symbol.values() if v > 0), reverse=True)
        if len(ev.pnl_by_symbol) >= 4 and pnl and pnl[0] / sum(pnl) > 0.6:
            add("CONCENTRATED_PNL", "warn", f"{pnl[0] / sum(pnl):.0%} of profit comes from one symbol")
        caveats = ["a backtest is a simulation: fills, liquidity and margin are modelled, not observed",
                   "the validation window is a single historical path"]
        if synthetic:
            caveats.insert(0, "SYNTHETIC DATA: this result says nothing about real markets")
        if any(x.code == "BELOW_BUY_AND_HOLD" for x in f):
            caveats.append("the strategy did not beat simply holding the universe")
        return AuditReport(hypothesis_id=ev.hypothesis_id, verdict="fail" if any(x.severity == "fail" for x in f) else "pass",
                           findings=f, reliability_caveats=caveats)


# =============================================================================== Risk & Portfolio
class RiskPortfolioAgent:
    name = "risk_portfolio"

    def __init__(self, max_drawdown: float = 0.25, max_leverage: float = 1.5, max_correlation: float = 0.9):
        self.max_drawdown, self.max_leverage, self.max_correlation = max_drawdown, max_leverage, max_correlation

    def review(self, ev: Evidence, daily: pd.Series | None, existing: dict[str, pd.Series]) -> RiskReport:
        f: list[Finding] = []
        m = ev.dev.get("metrics", {})
        dd = abs(min(m.get("max_drawdown") or 0, ev.validation.get("max_drawdown") or 0))
        if dd > self.max_drawdown:
            f.append(Finding(code="DRAWDOWN", severity="fail", message=f"max drawdown {dd:.1%} exceeds the {self.max_drawdown:.0%} limit"))
        lev = m.get("avg_gross_leverage") or 0
        if lev > self.max_leverage:
            f.append(Finding(code="LEVERAGE", severity="fail", message=f"average gross leverage {lev:.2f}x exceeds {self.max_leverage}x"))
        cv = m.get("cvar_95")
        if cv is not None and cv < -0.04:
            f.append(Finding(code="TAIL_RISK", severity="warn", message=f"daily CVaR(95) {cv:.1%}"))
        corrs = {}
        if daily is not None and len(daily) > 60:
            r = daily.pct_change().dropna()
            for hid, other in existing.items():
                o = other.pct_change().dropna()
                j = r.index.intersection(o.index)
                if len(j) > 60:
                    corrs[hid] = float(np.corrcoef(r[j], o[j])[0, 1])
            worst = max(corrs.items(), key=lambda kv: kv[1], default=None)
            if worst and worst[1] > self.max_correlation:
                f.append(Finding(code="REDUNDANT", severity="fail",
                                 message=f"daily-return correlation {worst[1]:.2f} with existing candidate {worst[0]}"))
        cap = m.get("capacity_estimate")
        return RiskReport(hypothesis_id=ev.hypothesis_id, verdict="fail" if any(x.severity == "fail" for x in f) else "pass",
                          findings=f, metrics={"max_drawdown": dd, "avg_gross_leverage": lev, "cvar_95": cv,
                                               "capacity_estimate": cap, "correlations": corrs})


# =============================================================================== Learning & Experiment
NEXT_STEP = {
    "COST_FRAGILE": ("reduce_turnover", "gross edge exists but does not survive doubled costs; test a lower-turnover form"),
    "NOT_BETTER_THAN_RANDOM": ("add_trend_filter", "entry timing adds nothing over random entry; test whether it is conditional on trend"),
    "NO_DEV_EDGE": (None, "no edge on development data; abandon this structure and explore a different family"),
    "VALIDATION_FAILED": (None, "the development result did not carry forward; treat it as overfit and do not re-tune it"),
    "VALIDATION_DECAY": (None, "performance decayed out of sample; do not re-tune — try a structurally different idea"),
    "PARAM_UNSTABLE": (None, "result depends on exact parameter values; a real effect should degrade gracefully"),
    "TOO_FEW_TRADES": (None, "too few trades to learn anything; this rule is too selective for the available history"),
    "DRAWDOWN": ("add_stop", "drawdown too deep; test whether a protective stop truncates the tail without killing the edge"),
    "REGIME_DEPENDENT": ("add_volatility_filter", "losses concentrate in the high-volatility regime; test a volatility condition"),
    "LOOKAHEAD": (None, "the implementation used future information; the generator or the spec is wrong and must be fixed"),
    "INSIGNIFICANT": (None, "result is statistically indistinguishable from zero"),
    "DSR_LOW": (None, "after accounting for the number of trials this is what luck alone would produce"),
}


class LearningAgent:
    """Learns which research directions are productive. It never sees sealed-set results and it
    never re-tunes parameters of a rejected hypothesis: it either proposes one structural variant
    that addresses the diagnosed failure, or moves to a different family."""
    name = "learning"

    def family_scores(self, memory: dict, priors: dict[str, float]) -> dict[str, dict]:
        stats = {f: {"tested": 0, "reward": 0.0} for f in FAMILIES}
        for h in memory["hypotheses"]:
            s = stats.get(h["family"])
            if s is None or h["status"] == "proposed":
                continue
            s["tested"] += 1
            s["reward"] += {"paper_candidate": 1.0, "advance_to_sealed": 1.0, "rejected_on_sealed": 0.6}.get(h["status"], 0.0)
        total = sum(s["tested"] for s in stats.values()) + 1
        for f, s in stats.items():
            mean = s["reward"] / s["tested"] if s["tested"] else 0.5
            s["mean_reward"] = mean
            s["ucb"] = mean * priors.get(f, 1.0) + 0.7 * math.sqrt(math.log(total + 1) / (s["tested"] + 1))
        return stats

    def choose_family(self, memory: dict, priors: dict[str, float]) -> tuple[str, dict]:
        scores = self.family_scores(memory, priors)
        return max(sorted(scores), key=lambda f: scores[f]["ucb"]), scores

    def reflect(self, spec: StrategySpec, decision: Decision, audit: AuditReport | None, risk: RiskReport | None) -> dict:
        codes = (audit.fail_codes if audit else []) + ([x.code for x in risk.findings if x.severity == "fail"] if risk else [])
        warn = [x.code for x in (audit.findings if audit else []) if x.severity == "warn"]
        directive, why = None, "no failure diagnosed"
        for c in codes + warn:
            if c in NEXT_STEP:
                directive, why = NEXT_STEP[c]
                break
        if decision.decision in ("promoted_to_paper_candidate", "advance_to_sealed"):
            outcome = "survived"
            text = f"[{spec.family}] {spec.id} survived development, validation and independent audit."
            nxt = "monitor in paper trading; do not modify it — any change is a new hypothesis"
            directive = None
        else:
            outcome = "rejected"
            text = f"[{spec.family}] {spec.id} rejected ({', '.join(codes) or 'no fail code'}): {why}."
            nxt = (f"test structural variant '{directive}' of {spec.id}" if directive and not spec.parent_id
                   else "move to a different hypothesis family")
            if spec.parent_id:
                directive = None      # one variant per lineage: no iterative patching of a failed idea
        return {"family": spec.family, "outcome": outcome, "text": text, "next_step": nxt, "directive": directive,
                "evidence": {"fail_codes": codes, "warn_codes": warn}}


# =============================================================================== Execution Readiness
class ExecutionReadinessAgent:
    name = "execution_readiness"

    def check(self, spec: StrategySpec, ev: Evidence, *, synthetic: bool, live_enabled: bool) -> ReadinessReport:
        c: list[Finding] = []
        add = lambda code, sev, msg: c.append(Finding(code=code, severity=sev, message=msg))
        add("ORDER_TYPES", "info", "uses MARKET, LIMIT and STOP (SL-M) orders, supported by the Kite Connect order API")
        add("PRODUCT", "info", "delivery (CNC) product, long-only: no short-selling or intraday leverage required")
        if synthetic:
            add("DATA", "fail", "validated on SYNTHETIC data only; must be re-validated on licensed NSE history")
        add("INSTRUMENT_SYNC", "fail", "broker instrument master not synchronised (no broker session)")
        add("STATIC_IP", "warn", "broker APIs require order traffic from a registered static IP; deployment not verified")
        add("COMPLIANCE", "fail", "exchange/broker algo registration and compliance checklist not completed "
                                  "(see docs/LIVE_READINESS_CHECKLIST.md)")
        add("HUMAN_APPROVAL", "fail", "no signed human approval exists for this strategy version")
        add("LIVE_FLAG", "info", f"LIVE_TRADING_ENABLED={'true' if live_enabled else 'false'}; agents cannot change it")
        return ReadinessReport(hypothesis_id=spec.id, status="PAPER_ONLY", checks=c, live_trading_enabled=live_enabled)
