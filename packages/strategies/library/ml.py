"""Machine-learning baseline with strictly causal walk-forward retraining."""
from __future__ import annotations

import numpy as np

from nseql.strategies.api import Strategy
from nseql.strategies.library.trend import _slot_value


def _features(c: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Feature row for each bar t using only data up to t. Returns (len(c), k) with NaN warm-up."""
    n = len(c)
    r = np.full(n, np.nan)
    r[1:] = np.diff(np.log(c))
    out = np.full((n, 6), np.nan)
    for i in range(60, n):
        w = r[i - 59:i + 1]
        out[i] = (r[i], w[-5:].sum(), w[-20:].sum(), w.sum(), w[-20:].std(),
                  np.log(v[i] / max(v[i - 19:i + 1].mean(), 1.0)))
    return out


class MLDirection(Strategy):
    """Logistic regression on trailing return/volatility/volume features predicting whether the
    next `horizon`-bar return is positive. Refit every `retrain` bars on a trailing window.

    Leakage control: the label for bar t needs close[t+horizon], so at decision time T only rows
    with t <= T-horizon are trainable — the last `horizon` rows are purged from every fit.
    """
    name = "ml_direction"
    family = "machine_learning"
    hypothesis = ("Simple return, volatility and volume features contain enough conditional information to rank "
                  "next-week direction; falsified if out-of-sample hit rate is not above the base rate and net "
                  "Sharpe does not exceed the momentum baseline.")
    default_params = {"horizon": 5, "train_window": 400, "retrain": 21, "threshold": 0.55, "max_positions": 5,
                      "invest": 0.95}
    param_grid = {"horizon": [3, 5, 10], "threshold": [0.52, 0.55, 0.6], "train_window": [250, 400]}

    def on_bar(self, ctx):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        p = self.params
        st = self.state
        st["i"] = st.get("i", -1) + 1
        syms = ctx.universe("EQ")
        feats = {}
        for s in syms:
            c = ctx.history(s, "close", p["train_window"] + 80)
            v = ctx.history(s, "volume", p["train_window"] + 80)
            if len(c) >= 200:
                feats[s] = (c, _features(c, v))
        if not feats:
            return
        if st["i"] % p["retrain"] == 0 or "model" not in st:
            X, y = [], []
            h = p["horizon"]
            for c, f in feats.values():
                fwd = c[h:] / c[:-h] - 1                       # label for rows 0 .. n-h-1 only
                rows = f[: len(c) - h]                         # purge the last h rows (labels not yet known)
                ok = ~np.isnan(rows).any(axis=1)
                X.append(rows[ok]); y.append((fwd[ok] > 0).astype(int))
            X, y = np.vstack(X), np.concatenate(y)
            if len(y) < 200 or len(set(y)) < 2:
                return
            st["model"] = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=500)).fit(X, y)
            st["base_rate"] = float(y.mean())
        model = st.get("model")
        if model is None:
            return
        age = st.setdefault("age", {})
        prob = {s: float(model.predict_proba(f[-1:])[0, 1]) for s, (c, f) in feats.items() if not np.isnan(f[-1]).any()}
        for s, q in ctx.positions().items():
            if q > 0:
                age[s] = age.get(s, 0) + 1
                if age[s] >= p["horizon"] and prob.get(s, 0) < p["threshold"]:
                    ctx.order_target(s, 0, tag="ml_exit")
                    age.pop(s, None)
        n_pos = len([q for q in ctx.positions().values() if q > 0])
        for s in sorted(prob, key=lambda s: (-prob[s], s)):
            if n_pos >= p["max_positions"]:
                break
            if prob[s] >= p["threshold"] and ctx.position(s) == 0 and not ctx.open_orders(s):
                ctx.order_target_value(s, _slot_value(ctx, p["max_positions"], p["invest"]), tag="ml_entry")
                n_pos += 1
