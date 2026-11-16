"""Effective-dated exchange / statutory rules.

`ChargeSchedule` answers "what did a trade of this kind cost on this date?".
The numbers live in rules/charges_in.json so they can be audited and corrected
without touching code; every entry records its source and whether it was verified.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from nseql.instruments.models import Instrument
from nseql.money import to_paise

RULES_DIR = Path(__file__).parent / "rules"


class RuleCoverageError(ValueError):
    """Raised when a rule is requested for a date outside the schedule's coverage."""


class _Schedule:
    def __init__(self, entries: list[dict]):
        self.entries = sorted(entries, key=lambda e: e["from"])
        self.starts = [date.fromisoformat(e["from"]) for e in self.entries]

    def at(self, d: date, allow_extrapolation: bool = False) -> dict:
        i = bisect_right(self.starts, d) - 1
        if i < 0:
            if not allow_extrapolation:
                raise RuleCoverageError(
                    f"no charge schedule covers {d}; earliest entry starts {self.starts[0]}. "
                    "Set allow_rule_extrapolation=true to use the earliest entry (flagged in the report).")
            i = 0
        return self.entries[i]


@dataclass(frozen=True, slots=True)
class BrokerageProfile:
    """Broker commission plan (configurable; default resembles an Indian discount broker)."""
    name: str = "discount"
    eq_delivery_pct: float = 0.0
    eq_delivery_cap: float = 0.0        # rupees per order; 0 with pct 0 => free
    eq_intraday_pct: float = 0.0003
    eq_intraday_cap: float = 20.0
    fut_pct: float = 0.0003
    fut_cap: float = 20.0
    opt_flat: float = 20.0              # rupees per executed order
    dp_charge_per_sell: float = 15.34   # depository charge per scrip per delivery-sell day (incl. GST)

    @staticmethod
    def zero() -> "BrokerageProfile":
        return BrokerageProfile("zero", 0, 0, 0, 0, 0, 0, 0, 0)


@dataclass(slots=True)
class Charges:
    brokerage: int = 0
    stt: int = 0
    exchange_txn: int = 0
    sebi: int = 0
    stamp: int = 0
    gst: int = 0
    dp: int = 0

    @property
    def total(self) -> int:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp + self.gst + self.dp

    def as_dict(self) -> dict[str, int]:
        return {"brokerage": self.brokerage, "stt": self.stt, "exchange_txn": self.exchange_txn,
                "sebi": self.sebi, "stamp": self.stamp, "gst": self.gst, "dp": self.dp, "total": self.total}

    def scaled(self, m: float) -> "Charges":
        return Charges(*(int(round(v * m)) for v in (self.brokerage, self.stt, self.exchange_txn,
                                                      self.sebi, self.stamp, self.gst, self.dp)))


@dataclass
class ChargeSchedule:
    """Computes all-in transaction charges in integer paise for a single fill."""
    brokerage: BrokerageProfile = field(default_factory=BrokerageProfile)
    allow_extrapolation: bool = False
    multiplier: float = 1.0             # cost-sensitivity analysis scales every component
    path: Path = RULES_DIR / "charges_in.json"
    extrapolated_dates: set = field(default_factory=set)

    def __post_init__(self):
        raw = _load(str(self.path))
        self._stt = _Schedule(raw["stt"]["schedule"])
        self._txn = _Schedule(raw["exchange_txn"]["schedule"])
        self._sebi = _Schedule(raw["sebi_fee"]["schedule"])
        self._stamp = _Schedule(raw["stamp_duty"]["schedule"])
        self._gst = _Schedule(raw["gst"]["schedule"])
        self.coverage_from = date.fromisoformat(raw["coverage_from"])

    def _at(self, sched: _Schedule, d: date) -> dict:
        if d < sched.starts[0] and self.allow_extrapolation:
            self.extrapolated_dates.add(d)
        return sched.at(d, self.allow_extrapolation)

    def brokerage_rupees(self, inst: Instrument, product: str, cum_value_rupees: float) -> float:
        """Brokerage owed for an order whose cumulative executed value is `cum_value_rupees`."""
        b = self.brokerage
        if inst.is_option:
            return b.opt_flat if cum_value_rupees > 0 else 0.0
        if inst.is_future:
            return min(b.fut_pct * cum_value_rupees, b.fut_cap)
        if product == "MIS":
            return min(b.eq_intraday_pct * cum_value_rupees, b.eq_intraday_cap)
        if b.eq_delivery_pct == 0:
            return 0.0
        v = b.eq_delivery_pct * cum_value_rupees
        return min(v, b.eq_delivery_cap) if b.eq_delivery_cap else v

    def compute(self, inst: Instrument, side: int, qty: int, price_paise: int, product: str, d: date,
                *, order_value_before_paise: int = 0, brokerage_paid_before_paise: int = 0,
                dp_applicable: bool = False) -> Charges:
        """Charges for one fill. `side` is +1 buy / -1 sell. Turnover is premium turnover for options.

        Brokerage caps apply per order, so the caller passes the order's executed value and
        brokerage already charged on earlier partial fills.
        """
        turnover = qty * price_paise                     # paise
        stt, txn, sebi, stamp, gst = (self._at(s, d) for s in
                                      (self._stt, self._txn, self._sebi, self._stamp, self._gst))
        c = Charges()
        owed = to_paise(self.brokerage_rupees(inst, product, (order_value_before_paise + turnover) / 100))
        c.brokerage = max(owed - brokerage_paid_before_paise, 0)
        if inst.is_option:
            c.stt = _r(turnover * stt["opt_sell"]) if side < 0 else 0
            c.exchange_txn = _r(turnover * txn["opt"])
            c.stamp = _r(turnover * stamp["opt"]) if side > 0 else 0
        elif inst.is_future:
            c.stt = _r(turnover * stt["fut_sell"]) if side < 0 else 0
            c.exchange_txn = _r(turnover * txn["fut"])
            c.stamp = _r(turnover * stamp["fut"]) if side > 0 else 0
        elif product == "MIS":
            c.stt = _r(turnover * stt["eq_intraday_sell"]) if side < 0 else 0
            c.exchange_txn = _r(turnover * txn["eq"])
            c.stamp = _r(turnover * stamp["eq_intraday"]) if side > 0 else 0
        else:
            c.stt = _r(turnover * stt["eq_delivery"])
            c.exchange_txn = _r(turnover * txn["eq"])
            c.stamp = _r(turnover * stamp["eq_delivery"]) if side > 0 else 0
            if dp_applicable and side < 0:
                c.dp = to_paise(self.brokerage.dp_charge_per_sell)
        c.sebi = _r(turnover * sebi["rate"])
        c.gst = _r((c.brokerage + c.exchange_txn + c.sebi) * gst["rate"])
        return c.scaled(self.multiplier) if self.multiplier != 1.0 else c

    def exercise_stt(self, inst: Instrument, qty: int, intrinsic_paise: int, d: date) -> Charges:
        """STT payable by the holder of a long option that expires in the money."""
        c = Charges(stt=_r(qty * intrinsic_paise * self._at(self._stt, d)["opt_exercise"]))
        return c.scaled(self.multiplier) if self.multiplier != 1.0 else c


def _r(x: float) -> int:
    return int(round(x))


@lru_cache(maxsize=8)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


@dataclass(frozen=True, slots=True)
class MarginRules:
    """Approximate margin requirements as a fraction of notional.

    NOT exchange SPAN: SPAN needs NSE's daily risk-parameter files, which are licensed data.
    These rates are conservative approximations of SPAN + exposure margin and are configurable.
    """
    index_fut: float = 0.12
    stock_fut: float = 0.20
    index_opt_short: float = 0.12
    stock_opt_short: float = 0.20
    eq_mis: float = 0.20     # 5x intraday leverage cap (SEBI peak-margin framework)
    eq_cnc: float = 1.00

    def fut_rate(self, inst: Instrument) -> float:
        return self.index_fut if inst.underlying_kind == "INDEX" else self.stock_fut

    def opt_short_rate(self, inst: Instrument) -> float:
        return self.index_opt_short if inst.underlying_kind == "INDEX" else self.stock_opt_short
