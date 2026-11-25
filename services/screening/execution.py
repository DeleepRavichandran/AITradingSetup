"""Order execution and the shared portfolio for the one-minute screening loop.

Two execution models:

* ``simple`` (the original slice): an order fills completely at the next bar's open, each configuration
  trades a fixed Rs 1,00,000 in its own sub-account. Kept because the historical-vs-streaming parity test
  is defined on it.
* ``realistic`` (the default for scheduled runs): the rules below.

Realistic fills
  * Liquidity: in each 1-minute bar, ALL of our orders in a symbol together may take at most
    ``participation`` (10%) of that bar's traded volume. Exits are served before entries, then earlier
    decisions first, then registry order: deterministic, so replays and recoveries match exactly.
  * Price: the bar's open, worse by a half-spread (5 bps) plus impact = 25 bps x sqrt(fill qty / bar
    volume), rounded to the 0.05 tick against us.
  * Partial fills: an entry that is not complete after ``entry_ttl_minutes`` (5) is cancelled for the rest
    (keeping what filled); an exit keeps working until the position is flat, overnight if it must.
  * Charges per fill from the dated schedule, with brokerage capped per ORDER across its fills and the
    depository charge once per sell order.

Shared portfolio (``PortfolioRules``), when enabled
  * One capital pot. A BUY is sized at ``per_trade_pct`` (2%) of current equity and only becomes an order
    if, after reserving its cash: cash minus reservations stays >= 0; the symbol's exposure across every
    configuration stays <= ``max_symbol_pct`` (10%) of equity; gross exposure <= ``max_gross_pct`` (100%,
    no leverage); open + pending positions < ``max_positions`` (50); and the day is not halted.
  * Daily loss cap: if equity falls ``daily_loss_pct`` (2%) below the day's opening equity, no new
    entries for the rest of the day (exits continue).
  * Every refused BUY is recorded with its reason. The strategy's signal is still recorded: the research
    ledger shows what it wanted; the paper ledger shows what the portfolio allowed.
  * Allocation order (``allocation``): ``ranked`` (default; see allocator.py) serves the minute's BUY
    signals best-first by forward evidence, costs, liquidity, correlation and concentration, and labels
    each accepted / rejected / deferred. ``first_come`` is the original registry-order baseline.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from nseql.instruments.models import Instrument, Kind
from nseql.money import to_paise
from nseql.screening.allocator import Allocator, AllocatorConfig
from nseql.screening.features import minute_of_session

NOTIONAL = 100_000.0


@dataclass(frozen=True)
class ExecConfig:
    participation: float = 0.10
    half_spread_bps: float = 5.0
    impact_bps: float = 25.0
    entry_ttl_minutes: int = 5
    tick: float = 0.05
    delay_bars: int = 0                 # extra bars between the decision bar and the first fill bar (stress test: 1)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PortfolioRules:
    capital: float = 1_000_000.0
    per_trade_pct: float = 0.02
    max_symbol_pct: float = 0.10
    max_gross_pct: float = 1.0
    max_positions: int = 50
    daily_loss_pct: float = 0.02
    cost_buffer: float = 0.003          # cash reserved above the order value for slippage and charges
    allocation: str = "ranked"          # "ranked" (allocator.py) or "first_come" (registry order, the old baseline)
    # churn limits (0 = off). Off by default so research replays stay comparable; the live paper account turns
    # them on (see LIVE_RULES_VERSION / live()). Learned on 2026-11-23: 708 trades by 12:23 on Rs 1 lakh.
    cooldown_minutes: int = 0           # no new entry in a symbol for this many minutes after any exit in it
    max_entries_per_symbol_day: int = 0
    max_entries_per_config_day: int = 0
    max_open_per_symbol: int = 0        # positions + working entries per symbol at a time
    group_gate_trades: int = 0          # refuse a strategy type (family x timeframe, all symbols) whose round trips
                                        # reach this count with an expected net return after costs below zero
    # experiment switches (off by default; see services/screening/experiment.py, hypotheses H1 and H2)
    fund_qualified_only: bool = False   # H1: hold cash unless the configuration's forward evidence is qualified (>= min_trades
                                        # round trips with a shrunk mean net return above zero)
    no_entry_first_minutes: int = 0     # H2: no new entries in the first N minutes of the session (opening-range noise)
    max_entries_day: int = 0            # operator limit: new buys per day for the whole account (0 = off); more only with the
                                        # operator's recorded approval (Executor.extra_entries, set from approvals/<day>.json)
    explore_fraction: float = 0.0       # H1b: configurations without qualified evidence trade at this fraction of the normal
                                        # size (exploration continues, so evidence can accrue); 0 = off (full size)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def live(cls, capital: float) -> "PortfolioRules":
        """Rules for the live paper account (version LIVE_RULES_VERSION).
        1.1 (2026-11-23): churn limits. 1.2 (operator choice A, 2026-11-23): fewer, larger trades - 10% of equity
        per buy (about Rs 10,000 on Rs 1 lakh, so stocks above Rs 2,000 become buyable), at most 8 positions,
        12% per stock (one 10% buy plus the cost buffer). 1.3 (2026-11-25): no new entries before 09:46."""
        return cls(capital=capital, per_trade_pct=0.10, max_positions=8, max_symbol_pct=0.12,
                   cooldown_minutes=15, max_entries_per_symbol_day=4, max_entries_per_config_day=1,
                   max_open_per_symbol=1, group_gate_trades=30, no_entry_first_minutes=30, max_entries_day=10)


# 1.3 (2026-11-25): no new entries before 09:46. Evidence: entries in the first 30 minutes lost on both real days
# (23 Nov: -8.94 INR per trade over 120 trades; 25 Nov: -50.91 INR per trade over 15 trades, 95% range below zero) and
# the opening blackout (H2) was accepted on the synthetic walk-forward validation (docs/REVIEW_1376b45.md). Exits are not
# affected. Risk limits are unchanged.
# 1.4 (2026-11-25, operator's decision): at most 10 new trades (buys) per day for the whole account; more only with the
# operator's approval and a written reason (POST /api/risk/extra-trades, operator key only; audited). Exits are never blocked.
LIVE_RULES_VERSION = "1.4"

# Execution assumptions (review of 1376b45, 2026-11-25). A ledger produced under an earlier version keeps its rows; the
# ledger audit flags it for recomputation (results are not comparable across versions).
EXECUTION_VERSION = "2.0"
EXECUTION_CHANGES = {
    "2.0": ["kill switch / daily loss halt cancels working buys (and the rest of partial buys) BEFORE the next bar; exits continue",
            "symbol, gross and cash limits rechecked at the actual fill price, with the dated charges; reservations cover the "
            "worst-case price plus charges",
            "live minutes decided more than 55 s after their candle closed are catch-up: never traded (no retrospective fills)",
            "standalone configurations (no shared portfolio) each get their own liquidity allowance",
            "optional execution delay (ExecConfig.delay_bars) for stress tests"],
}


def _tick(px: float, tick: float, up: bool) -> float:
    k = px / tick
    return round((math.ceil(k - 1e-9) if up else math.floor(k + 1e-9)) * tick, 2)


class Executor:
    """Owns the order and cash state; mutates the engine's position arrays."""

    STATE = ("pend_qty", "pend_req", "pend_ttl", "ord_val", "ord_brk", "ord_ch", "ord_fill", "ord_cost", "ord_res", "ord_real", "ord_last",
             "ord_basis", "cfg_day_n")
    SYM_STATE = ("sym_exit", "sym_day_n")                    # per symbol: minute of the last exit, entries today
    NO_EXIT = -(10 ** 12)

    def __init__(self, eng, cfg: ExecConfig | None, rules: PortfolioRules | None, alloc_cfg: AllocatorConfig | None = None):
        self.e, self.cfg, self.rules = eng, cfg, rules
        if rules and rules.allocation not in ("ranked", "first_come"):
            raise ValueError(f"unknown allocation {rules.allocation!r}")
        n = len(eng.reg)
        self.group = eng.reg.fam.astype(np.int64) * 16 + eng.reg.tf.astype(np.int64)
        self.alloc = Allocator(n, len(eng.reg.symbols), 16 * 16, alloc_cfg)
        for k in self.STATE:
            setattr(self, k, np.zeros(n, dtype=np.int64))
        self.sym_exit = np.full(len(eng.reg.symbols), self.NO_EXIT, dtype=np.int64)
        self.sym_day_n = np.zeros(len(eng.reg.symbols), dtype=np.int64)
        self.cash = to_paise(rules.capital) if rules else 0
        self.reserved = 0
        self.day = -1
        self.day_start = self.cash
        self.halted_day = False
        self.day_entries = 0                            # new buys accepted today (account-wide)
        self.extra_entries = 0                          # extra buys the operator approved for today
        self.events: list[dict] = []
        self.qty_cap: dict[int, int] | None = None     # optional per-order share cap set by a strategy (risk-based sizing)

    @property
    def mode(self) -> str:
        return "realistic" if self.cfg else "simple"

    # ------------------------------------------------------------------ valuation
    def equity(self) -> int:
        e = self.e
        held = e.pos == 1
        if not held.any():
            return self.cash
        px = np.nan_to_num(e.fe.last_close[e.reg.sym[held]], nan=0.0)
        return int(self.cash + np.round(px * 100).astype(np.int64) @ e.qty[held])

    def new_day(self, ts: pd.Timestamp) -> None:
        d = ts.toordinal()
        if d != self.day:
            self.day, self.day_start, self.halted_day = d, self.equity(), False
            self.day_entries = 0
            self.sym_day_n[:] = 0
            self.cfg_day_n[:] = 0

    def check_loss(self, ts) -> dict | None:
        if not self.rules or self.halted_day:
            return None
        eq = self.equity()
        if eq <= self.day_start * (1 - self.rules.daily_loss_pct):
            self.halted_day = True
            ev = {"ts": str(ts), "event": "DAILY_LOSS_LIMIT", "equity_inr": eq / 100, "day_start_inr": self.day_start / 100,
                  "detail": f"equity fell {self.rules.daily_loss_pct:.1%} below the day's opening; no new entries today"}
            self.events.append(ev)
            return ev
        return None

    # ------------------------------------------------------------------ order submission
    def submit(self, actionable: list[int], decisions: dict[int, str], minute_em: int, allow: bool,
               block_reason: str = "kill switch engaged", reduce_only: bool = False) -> list[dict]:
        """Turn BUY/EXIT decisions into orders. Returns order records (accepted and refused).

        allow=False refuses new orders. With reduce_only (the kill switch) exits are still accepted: they only reduce
        exposure, and a protective stop must not be switched off by a halt. Working buys are cancelled in fill(),
        BEFORE the next bar is matched (see `block`)."""
        e, r, out = self.e, self.e.reg, []
        if not allow:
            keep = [i for i in actionable if reduce_only and decisions[i] != "BUY"]
            out = [{"config": i, "side": "BUY" if decisions[i] == "BUY" else "SELL", "decided": minute_em, "status": "rejected",
                    "qty": 0, "reason": block_reason} for i in actionable if i not in keep]
            if not keep:
                return out
            actionable, decisions = keep, {i: decisions[i] for i in keep}
        eq = self.equity() if self.rules else 0
        close = e.fe.last_close
        buys = []
        for i in actionable:
            if decisions[i] != "BUY":
                e.pend[i], e.pend_key[i] = -1, minute_em
                self.pend_qty[i] = self.pend_req[i] = e.qty[i] if self.cfg else 0
                self._reset_order(i)
                out.append({"config": i, "side": "SELL", "decided": minute_em, "status": "pending", "qty": int(e.qty[i]), "reason": "",
                            "alloc": "accepted", "alloc_reason": "exits are always served", "score": None})
            else:
                buys.append(i)
        if not buys:
            return out
        est = self.alloc.estimates(self.group)
        if self.rules is None:
            for i in buys:
                q = 0
                if self.cfg is not None:                             # realistic fills, own Rs 1,00,000 per configuration
                    px = float(close[r.sym[i]])
                    q = int(NOTIONAL // px) if px > 0 else 0
                    if q <= 0:
                        out.append(self._buy_rec(i, minute_em, "rejected", q, "price above trade size", "rejected"))
                        continue
                e.pend[i], e.pend_key[i] = 1, minute_em                 # simple model: sized at the fill
                self._reset_order(i)
                self.pend_qty[i] = self.pend_req[i] = q
                self.pend_ttl[i] = self.cfg.entry_ttl_minutes if self.cfg else 0
                self.alloc.record_entry(i, float(est["p_win"][i]))
                out.append(self._buy_rec(i, minute_em, "pending", q, "", "accepted", "no shared portfolio"))
            return out

        rules, lim = self.rules, eq / 100
        held_sym = np.bincount(r.sym, weights=np.where(e.pos == 1, e.qty * np.nan_to_num(close[r.sym]), 0.0), minlength=len(r.symbols))
        res_sym = np.bincount(r.sym, weights=np.where(e.pend == 1, self.ord_res / 100.0, 0.0), minlength=len(r.symbols))
        gross = float(held_sym.sum() + res_sym.sum())
        n_pos = int(((e.pos == 1) | (e.pend == 1)).sum())
        free = (self.cash - self.reserved) / 100
        start = (free, n_pos, held_sym + res_sym, gross)                   # limits as they stood before this minute's entries
        b = np.array(buys, dtype=np.int64)
        px = close[r.sym[b]]
        q0 = np.where(px > 0, (rules.per_trade_pct * lim) // np.where(px > 0, px, 1), 0).astype(np.int64)
        if self.qty_cap:                           # a strategy's own size limit; the portfolio limits below still apply
            q0 = np.minimum(q0, np.array([self.qty_cap.get(int(i), int(q)) for i, q in zip(b, q0)], dtype=np.int64))
        ranked = rules.allocation == "ranked"
        info: dict[int, dict] = {}
        if ranked:
            order, info = self._rank(b, px, q0, est, held_sym, lim)
        else:
            order = list(buys)                                         # registry order
        if rules.explore_fraction:                 # H1b: small size until the configuration's forward evidence qualifies
            q0 = np.where(est["qualified"][b], q0, np.floor(q0 * rules.explore_fraction)).astype(np.int64)
        q_of = dict(zip(buys, (int(x) for x in q0)))
        earlier = "higher-ranked" if ranked else "earlier"
        new_in_sym: dict[int, int] = {}
        open_sym = np.bincount(r.sym, weights=((e.pos == 1) | (e.pend == 1)).astype(float), minlength=len(r.symbols))
        for i in order:
            s = int(r.sym[i])
            d = info.get(i, {})
            q = int(d["q"]) if ranked else q_of[i]
            p = float(close[s])
            need = 0.0                                # exact worst-case reservation: computed only for candidates that reach the limit checks
            why, kind = "", "rejected"
            if d.get("why"):
                why = d["why"]
            elif q <= 0:
                why = "price above trade size"
            elif self.halted_day:
                why = "daily loss limit reached"
            elif rules.max_entries_day and self.day_entries >= rules.max_entries_day + self.extra_entries:
                why = (f"daily trade limit: {self.day_entries} buys today (limit {rules.max_entries_day}"
                       + (f" + {self.extra_entries} approved" if self.extra_entries else "") + "); more need the operator's approval")
            elif rules.no_entry_first_minutes and minute_of_session(pd.Timestamp(int(minute_em) * 60_000_000_000)) <= rules.no_entry_first_minutes:
                why = f"no entries in the first {rules.no_entry_first_minutes} minutes of the session"
            elif rules.fund_qualified_only and not bool(est["qualified"][i]):
                why = "holding cash: no qualified forward evidence that this configuration earns more than its costs"
            elif rules.cooldown_minutes and minute_em - self.sym_exit[s] < rules.cooldown_minutes:
                why = f"cool-down: this stock was sold less than {rules.cooldown_minutes} min ago"
            elif rules.max_entries_per_config_day and self.cfg_day_n[i] >= rules.max_entries_per_config_day:
                why = f"daily cap: this strategy already bought {rules.max_entries_per_config_day}x today"
            elif rules.max_entries_per_symbol_day and self.sym_day_n[s] >= rules.max_entries_per_symbol_day:
                why = f"daily cap: this stock already bought {rules.max_entries_per_symbol_day}x today"
            elif rules.max_open_per_symbol and open_sym[s] >= rules.max_open_per_symbol:
                why = "one at a time: already holding or buying this stock"
            elif ranked and new_in_sym.get(s, 0) >= self.cfg_alloc.max_new_per_symbol:
                why, kind = "redundant: a higher-ranked configuration already entered this symbol this minute", "deferred"
            else:
                # the position check does not depend on the reservation; when it decides, skip the exact (charge-model) estimate
                need = q * p * (1 + rules.cost_buffer) if n_pos >= rules.max_positions else self._need(i, q, p, minute_em, rules.cost_buffer)
                checks = (("max_positions", n_pos >= rules.max_positions, start[1] >= rules.max_positions, f"max {rules.max_positions} positions"),
                          ("cash", free < need, start[0] < need, "not enough free cash"),
                          ("symbol", held_sym[s] + res_sym[s] + need > rules.max_symbol_pct * lim,
                           start[2][s] + need > rules.max_symbol_pct * lim, f"symbol exposure above {rules.max_symbol_pct:.0%}"),
                          ("gross", gross + need > rules.max_gross_pct * lim, start[3] + need > rules.max_gross_pct * lim,
                           f"gross exposure above {rules.max_gross_pct:.0%}"))
                for _, now_fails, before_fails, text in checks:
                    if now_fails:
                        if before_fails:
                            why = text
                        else:
                            why, kind = f"{text} after {earlier} signals this minute", "deferred"
                        break
            expl = d["explain"] if ranked else "registry order (first come)"
            if why:
                out.append(self._buy_rec(i, minute_em, "rejected", q, why, kind, expl, d.get("score")))
                continue
            e.pend[i], e.pend_key[i] = 1, minute_em
            self._reset_order(i)
            self.pend_qty[i] = self.pend_req[i] = q
            self.pend_ttl[i] = self.cfg.entry_ttl_minutes if self.cfg else 0
            res = to_paise(need)
            self.ord_res[i] = res
            self.reserved += res
            free -= need
            res_sym[s] += need
            gross += need
            n_pos += 1
            new_in_sym[s] = new_in_sym.get(s, 0) + 1
            open_sym[s] += 1
            self.sym_day_n[s] += 1
            self.cfg_day_n[i] += 1
            self.day_entries += 1
            self.alloc.record_entry(i, float(est["p_win"][i]))
            out.append(self._buy_rec(i, minute_em, "pending", q, "", "accepted", expl, d.get("score")))
        return out

    def _worst_px(self, q: int, p: float, vol: float) -> float:
        """Highest plausible fill price for a buy decided at close p: spread, impact for q shares, one tick of rounding."""
        if self.cfg is None:
            return p
        frac = (self.cfg.half_spread_bps + self.cfg.impact_bps * math.sqrt(q / max(vol, 1.0))) / 1e4
        return _tick(p * (1 + frac), self.cfg.tick, True) + self.cfg.tick

    def _charges_for(self, i: int, side: int, q: int, px: float, day) -> int:
        r = self.e.reg
        if q <= 0:
            return 0
        return int(self.e._charges.compute(Instrument(r.symbols[r.sym[i]], Kind.EQ), side, q, to_paise(px), "MIS" if r.intraday[i] else "CNC",
                                           day).total)

    def _need(self, i: int, q: int, p: float, minute_em: int, buffer: float) -> float:
        """Rupees reserved for a buy: the larger of the flat buffer and the worst-case price plus the dated charges for q shares."""
        if q <= 0:
            return 0.0
        day = pd.Timestamp(int(minute_em) * 60_000_000_000).date()
        wp = self._worst_px(q, p, float(self.alloc.ewma_vol[self.e.reg.sym[i]]))
        return max(q * p * (1 + buffer), q * wp + self._charges_for(i, 1, q, wp, day) / 100)

    @property
    def cfg_alloc(self) -> AllocatorConfig:
        return self.alloc.cfg

    @staticmethod
    def _buy_rec(i, em, status, q, reason, alloc, explain=None, score=None) -> dict:
        return {"config": int(i), "side": "BUY", "decided": em, "status": status, "qty": int(q), "reason": reason, "alloc": alloc,
                "alloc_reason": reason or explain or "", "score": None if score is None else round(float(score), 3)}

    def _rank(self, b: np.ndarray, px: np.ndarray, q0: np.ndarray, est: dict, held_sym: np.ndarray, lim: float):
        """Order this minute's BUY candidates best-first. Uses only state at the decision minute."""
        e, r, a, c = self.e, self.e.reg, self.alloc, self.alloc.cfg
        sym = r.sym[b]
        vol = a.ewma_vol[sym]
        ttl = self.cfg.entry_ttl_minutes if self.cfg else 1
        part = self.cfg.participation if self.cfg else 1.0
        cap = np.floor(part * vol * ttl).astype(np.int64)
        q = np.minimum(q0, cap)
        impact_bps = (self.cfg.impact_bps if self.cfg else 0.0) * np.sqrt(q / np.maximum(vol, 1.0)) * 2      # in and out
        rel = a.reliability()
        mu_bps = est["mu"][b] * 1e4
        corr = a.correlation(held_sym)[sym]
        sym_share = held_sym[sym] / max(self.rules.max_symbol_pct * lim, 1e-9)
        open_fam = np.bincount(r.fam[e.pos == 1], minlength=int(r.fam.max()) + 1)
        fam_share = open_fam[r.fam[b]] / max(open_fam.sum(), 1)
        score = rel * mu_bps - impact_bps - c.corr_bps * np.maximum(corr, 0) - c.conc_bps * sym_share - c.family_bps * fam_share
        qual = est["qualified"][b]
        idx = sorted(range(len(b)), key=lambda j: (not qual[j], -score[j], int(b[j])))
        info = {}
        for rank, j in enumerate(idx, 1):
            i = int(b[j])
            note = "" if q[j] >= q0[j] else f"; size cut from {int(q0[j])} to {int(q[j])} shares by liquidity"
            exp_net = rel * mu_bps[j] - impact_bps[j]
            why = "no liquidity: recent volume too low" if q[j] <= 0 < q0[j] else ""
            if not why and c.reject_negative and a.ev_n[i] >= c.min_trades and exp_net < 0:
                why = f"expected net return after costs below zero over at least {c.min_trades} forward trades"
            gg = self.rules.group_gate_trades
            if not why and gg and est["group_n"][self.group[i]] >= gg:
                g_net = rel * est["group_mu"][self.group[i]] * 1e4 - impact_bps[j]
                if g_net < 0:
                    why = "strategy type losing after costs (this rule family and timeframe, all stocks)"
            info[i] = {"q": int(q[j]), "score": float(score[j]), "why": why,
                       "explain": (f"rank {rank}/{len(b)}{' (qualified)' if qual[j] else ''}: score {score[j]:.1f} bps = "
                                   f"expected net {rel * mu_bps[j]:.1f} (from {int(a.ev_n[i])} trades, confidence {est['confidence'][i]:.2f}) "
                                   f"- impact {impact_bps[j]:.1f} - correlation {c.corr_bps * max(corr[j], 0):.1f} "
                                   f"- concentration {c.conc_bps * sym_share[j] + c.family_bps * fam_share[j]:.1f}{note}")}
        return [int(b[j]) for j in idx], info

    def _reset_order(self, i: int) -> None:
        for k in ("ord_val", "ord_brk", "ord_ch", "ord_fill", "ord_cost", "ord_res", "ord_real", "ord_basis"):
            getattr(self, k)[i] = 0

    # ------------------------------------------------------------------ fills
    def fill(self, ts: pd.Timestamp, has_bar: np.ndarray, open_px: np.ndarray, volume: np.ndarray,
             block: str | None = None) -> tuple[list[dict], list[dict]]:
        """Execute working orders against this minute's bars. Returns (fills, completed orders).

        Before anything is matched: if `block` is set (kill switch) or the day is halted by the loss limit, every
        working BUY is cancelled, including the unfilled rest of a partial one. Working exits continue: they only
        reduce exposure. An order fills no earlier than `delay_bars` + 1 bars after its decision bar."""
        e, r = self.e, self.e.reg
        fills, done = [], []
        why = block or ("daily loss limit reached" if self.halted_day else None)
        if why:
            for i in np.flatnonzero(e.pend == 1):
                i = int(i)
                done.append(self._close_order(i, "partial" if self.ord_fill[i] > 0 else "cancelled", f"{why}: rest of the buy cancelled before the next bar"))
        em = int(ts.value // 60_000_000_000)
        delay = self.cfg.delay_bars if self.cfg else 0
        idx = np.flatnonzero((e.pend != 0) & has_bar[r.sym] & (e.pend_key < em - delay))
        if not len(idx):
            return fills, self._expire(ts, done)
        if self.cfg is None:                                        # simple: whole order at the open
            for i in idx:
                i = int(i)
                q = int(NOTIONAL // open_px[r.sym[i]]) if e.pend[i] > 0 else int(e.qty[i])
                if q <= 0:
                    done.append(self._close_order(i, "cancelled", "price above trade size"))
                    continue
                f = self._apply(i, int(e.pend[i]), q, float(open_px[r.sym[i]]), ts)
                fills.append(f)
                done.append(self._close_order(i, "filled", ""))
            return fills, done
        order = sorted(idx, key=lambda i: (int(r.sym[i]), 0 if e.pend[i] < 0 else 1, int(e.pend_key[i]), int(i)))
        cap_left: dict[int, int] = {}
        lim = self._fill_limits(open_px) if self.rules else None
        for i in order:
            i, s = int(i), int(r.sym[i])
            # one liquidity allowance per symbol for orders competing inside ONE portfolio; without a shared portfolio every
            # configuration is its own account and gets its own allowance (its result must not depend on unrelated configurations)
            key = s if self.rules else -1 - i
            if key not in cap_left:
                cap_left[key] = int(self.cfg.participation * volume[s])
            q = int(min(self.pend_qty[i], cap_left[key]))
            if q <= 0:
                continue
            side, why = int(e.pend[i]), ""
            px_of = lambda k: _tick(open_px[s] * (1 + (self.cfg.half_spread_bps + self.cfg.impact_bps * math.sqrt(k / max(volume[s], 1.0))) / 1e4)
                                    if side > 0 else open_px[s] * (1 - (self.cfg.half_spread_bps + self.cfg.impact_bps * math.sqrt(k / max(volume[s], 1.0))) / 1e4),
                                    self.cfg.tick, side > 0)
            px = px_of(q)
            if side > 0 and lim is not None:
                q, why = self._cap_at_fill(i, s, q, px, px_of, ts, lim)
                if q <= 0:
                    done.append(self._close_order(i, "partial" if self.ord_fill[i] > 0 else "cancelled", why))
                    continue
                px = px_of(q)
            fills.append(self._apply(i, side, q, px, ts))
            if side > 0 and lim is not None:
                lim["sym"][s] += q * px
                lim["gross"] += q * px
            cap_left[key] -= q
            self.pend_qty[i] -= q
            if self.pend_qty[i] == 0 or (side > 0 and lim is not None and why):
                done.append(self._close_order(i, "filled" if self.pend_qty[i] == 0 else "partial", "" if self.pend_qty[i] == 0 else why))
        return fills, self._expire(ts, done)

    def _fill_limits(self, open_px: np.ndarray) -> dict:
        """Exposure at this bar's prices (held shares at the open where a bar exists, else the last close)."""
        e, r = self.e, self.e.reg
        mark = np.where(np.isnan(open_px), np.nan_to_num(e.fe.last_close), open_px)
        held = np.where(e.pos == 1, e.qty * mark[r.sym], 0.0)
        eq = self.cash / 100 + float(held.sum())
        res = np.bincount(r.sym, weights=np.where(e.pend == 1, self.ord_res, 0), minlength=len(r.symbols))   # paise; only shrinks while matching
        return {"sym": np.bincount(r.sym, weights=held, minlength=len(r.symbols)), "gross": float(held.sum()), "equity": eq, "res_sym": res}

    def _cap_at_fill(self, i: int, s: int, q: int, px: float, px_of, ts, lim) -> tuple[int, str]:
        """Largest q' <= q this buy may fill at the actual price: free cash after the dated charges (other working
        orders' reservations stay untouched), the symbol cap and the gross cap at this bar's prices."""
        rules = self.rules
        other_res = (self.reserved - int(self.ord_res[i])) / 100
        res_sym = (float(lim["res_sym"][s]) - float(self.ord_res[i])) / 100
        room = {"not enough cash at the fill price (after charges)": self.cash / 100 - other_res,
                f"symbol exposure above {rules.max_symbol_pct:.0%} at the fill price": rules.max_symbol_pct * lim["equity"] - lim["sym"][s] - res_sym,
                f"gross exposure above {rules.max_gross_pct:.0%} at the fill price": rules.max_gross_pct * lim["equity"] - lim["gross"] - (other_res - 0)}
        why = ""
        k = q
        p0 = px_of(q)                                 # cheap sufficient check: charges never exceed 0.5% + Rs 50 of an equity order
        if all(left >= (q * p0 * 1.005 + 50 if w.startswith("not enough cash") else q * p0) for w, left in room.items()):
            return q, ""
        while k > 0:
            p = px_of(k)
            cost = k * p + self._charges_for(i, 1, k, p, ts.date()) / 100
            value = k * p
            bad = [w for w, left in room.items() if (cost if w.startswith("not enough cash") else value) > left + 1e-9]
            if not bad:
                break
            why = bad[0]
            k = min(k - 1, int(max(min(left for left in room.values()), 0) // max(p, 1e-9))) if k > 1 else 0
        return k, (why if k < q else "")

    def _expire(self, ts, done: list[dict]) -> list[dict]:
        """Count down unfinished entries; cancel the rest of any whose time is up."""
        e = self.e
        if self.cfg is None:
            return done
        live = np.flatnonzero((e.pend == 1) & (self.pend_qty > 0))
        self.pend_ttl[live] -= 1
        for i in live[self.pend_ttl[live] <= 0]:
            i = int(i)
            done.append(self._close_order(i, "partial" if self.ord_fill[i] > 0 else "cancelled",
                                          f"{int(self.pend_qty[i])} of {int(self.pend_req[i])} unfilled after {self.cfg.entry_ttl_minutes} min"))
        return done

    def _apply(self, i: int, side: int, q: int, px: float, ts: pd.Timestamp) -> dict:
        e, r = self.e, self.e.reg
        inst = Instrument(r.symbols[r.sym[i]], Kind.EQ)
        product = "MIS" if r.intraday[i] else "CNC"
        first = self.ord_fill[i] == 0
        ch = e._charges.compute(inst, side, q, to_paise(px), product, ts.date(), order_value_before_paise=int(self.ord_val[i]),
                                brokerage_paid_before_paise=int(self.ord_brk[i]), dp_applicable=side < 0 and first and product == "CNC")
        val = q * to_paise(px)
        self.ord_val[i] += val
        self.ord_brk[i] += ch.brokerage
        self.ord_ch[i] += ch.total
        self.ord_fill[i] += q
        self.ord_cost[i] += val
        realized = 0
        if side > 0:
            old = int(e.qty[i])
            e.entry_px[i] = (e.entry_px[i] * old + px * q) / (old + q)
            e.qty[i] = old + q
            e.entry_ch[i] += ch.total
            e.pos[i] = 1
            e.stop_px[i] = e.entry_px[i] * (1 - r.stop[i])
            self.cash -= val + ch.total
            if self.rules and self.ord_res[i]:
                rel = int(self.ord_res[i] * q // max(int(self.pend_req[i]), 1))
                rel = min(rel, int(self.ord_res[i]))
                self.ord_res[i] -= rel
                self.reserved -= rel
        else:
            held = int(e.qty[i])
            alloc = int(e.entry_ch[i] * q // held) if q < held else int(e.entry_ch[i])
            realized = int((to_paise(px) - to_paise(e.entry_px[i])) * q - alloc - ch.total)
            self.ord_real[i] += realized
            self.ord_basis[i] += to_paise(e.entry_px[i]) * q
            e.qty[i] = held - q
            e.entry_ch[i] -= alloc
            self.cash += val - ch.total
            self.sym_exit[r.sym[i]] = int(ts.value // 60_000_000_000)
            if e.qty[i] == 0:
                e.pos[i], e.entry_px[i], e.entry_ch[i], e.stop_px[i] = 0, 0.0, 0, 0.0
        self.ord_last[i] = int(ts.value // 60_000_000_000)
        return {"config": i, "side": "BUY" if side > 0 else "SELL", "decided": int(e.pend_key[i]), "ts": ts, "qty": q, "price": px,
                "charges_paise": int(ch.total), "realized_paise": realized, "product": product}

    def _close_order(self, i: int, status: str, reason: str) -> dict:
        e = self.e
        side = "BUY" if e.pend[i] > 0 else "SELL"
        if self.rules and self.ord_res[i]:
            self.reserved -= int(self.ord_res[i])
            self.ord_res[i] = 0
        filled = int(self.ord_fill[i])
        if side == "SELL" and self.ord_basis[i] > 0:                 # a round trip finished: forward evidence for ranking
            self.alloc.record_round_trip(i, float(self.ord_real[i]) / float(self.ord_basis[i]))
        rec = {"config": i, "side": side, "decided": int(e.pend_key[i]), "status": status, "reason": reason, "filled_qty": filled,
               "requested_qty": int(self.pend_req[i]) if self.cfg or self.rules else filled,
               "avg_price": (self.ord_cost[i] / filled / 100) if filled else None, "charges_paise": int(self.ord_ch[i]),
               "realized_paise": int(self.ord_real[i]), "last_fill": int(self.ord_last[i]) if filled else None,
               "product": "MIS" if e.reg.intraday[i] else "CNC"}
        e.pend[i] = 0
        self.pend_qty[i] = self.pend_req[i] = self.pend_ttl[i] = 0
        return rec

    # ------------------------------------------------------------------ reporting / checkpoint
    def snapshot(self) -> dict:
        e, r = self.e, self.e.reg
        held = e.pos == 1
        by_sym = np.bincount(r.sym, weights=np.where(held, e.qty * np.nan_to_num(e.fe.last_close[r.sym]), 0.0), minlength=len(r.symbols))
        eq = self.equity()
        top = np.argsort(-by_sym)[:10]
        return {"mode": self.mode, "shared_capital": bool(self.rules), "cash_inr": self.cash / 100, "reserved_inr": self.reserved / 100,
                "equity_inr": eq / 100, "day_start_equity_inr": self.day_start / 100, "halted_today": self.halted_day,
                "entries_today": self.day_entries, "entries_allowed_today": (self.rules.max_entries_day + self.extra_entries)
                if self.rules and self.rules.max_entries_day else None,
                "open_positions": int(held.sum()), "working_orders": int((e.pend != 0).sum()),
                "gross_exposure_inr": float(by_sym.sum()), "top_symbols": [{"symbol": r.symbols[s], "value_inr": round(float(by_sym[s]), 2),
                                                                             "pct_equity": round(float(by_sym[s]) / max(eq / 100, 1), 4)}
                                                                            for s in top if by_sym[s] > 0],
                "rules": self.rules.to_dict() if self.rules else None, "execution": self.cfg.to_dict() if self.cfg else None,
                "allocation": {"method": self.rules.allocation if self.rules else "none", "settings": self.alloc.cfg.to_dict(),
                               "configs_with_evidence": int((self.alloc.ev_n > 0).sum()), "round_trips": int(self.alloc.ev_n.sum()),
                               "qualified_configs": int(self.alloc.estimates(self.group)["qualified"].sum()),
                               "calibration": self.alloc.calibration()}}

    def state(self) -> dict:
        st = {f"x_{k}": getattr(self, k) for k in (*self.STATE, *self.SYM_STATE)}
        st["x_scalars"] = np.array([self.cash, self.reserved, self.day, self.day_start, int(self.halted_day), self.day_entries], dtype=np.int64)
        st["x_mode"] = np.array([int(self.cfg is not None), int(self.rules is not None)])
        st.update(self.alloc.state())
        return st

    def load(self, st: dict) -> None:
        if "x_mode" in st and tuple(int(x) for x in st["x_mode"]) != (int(self.cfg is not None), int(self.rules is not None)):
            raise ValueError("checkpoint was taken with a different execution model / portfolio setting")
        for k in self.STATE:
            v = st.get(f"x_{k}")
            setattr(self, k, np.array(v) if v is not None else np.zeros(len(self.e.reg), dtype=np.int64))   # older checkpoints
        for k in self.SYM_STATE:
            v = st.get(f"x_{k}")
            if v is not None:
                setattr(self, k, np.array(v, dtype=np.int64))
        self.alloc.load(st)
        sc = [int(x) for x in np.asarray(st["x_scalars"])]
        self.cash, self.reserved, self.day, self.day_start, h = sc[:5]
        self.day_entries = sc[5] if len(sc) > 5 else 0                 # checkpoints before 1.4 had no counter
        self.halted_day = bool(h)
