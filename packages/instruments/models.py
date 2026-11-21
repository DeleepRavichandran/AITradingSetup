"""Instrument master domain model."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum


class Kind(StrEnum):
    EQ = "EQ"          # cash equity
    ETF = "ETF"
    INDEX = "INDEX"    # not tradable; reference / settlement underlying
    FUT = "FUT"
    OPT = "OPT"
    CUR_FUT = "CUR_FUT"    # currency / commodity / IRD: modelled in the master, no adapter yet
    COM_FUT = "COM_FUT"
    IRD_FUT = "IRD_FUT"


CASH_KINDS = (Kind.EQ, Kind.ETF)
FUTURE_KINDS = (Kind.FUT, Kind.CUR_FUT, Kind.COM_FUT, Kind.IRD_FUT)


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    kind: Kind
    exchange: str = "NSE"
    segment: str = "CM"
    lot_size: int = 1
    tick_size: float = 0.05
    underlying: str | None = None
    underlying_kind: str | None = None   # "INDEX" or "EQ" for derivatives
    expiry: date | None = None
    strike: float | None = None
    option_type: str | None = None       # "CE" / "PE"
    listed_from: date | None = None
    listed_to: date | None = None
    price_band_pct: float | None = None  # e.g. 0.10 for a 10% daily band; None = no band
    sector: str | None = None
    isin: str | None = None
    settlement: str = "cash"             # "cash" or "physical"

    @property
    def tradable(self) -> bool:
        return self.kind is not Kind.INDEX

    @property
    def is_cash(self) -> bool:
        return self.kind in CASH_KINDS

    @property
    def is_future(self) -> bool:
        return self.kind in FUTURE_KINDS

    @property
    def is_option(self) -> bool:
        return self.kind is Kind.OPT

    @property
    def is_derivative(self) -> bool:
        return self.is_future or self.is_option

    def listed_on(self, d: date) -> bool:
        if self.listed_from and d < self.listed_from:
            return False
        if self.listed_to and d > self.listed_to:
            return False
        if self.expiry and d > self.expiry:
            return False
        return True

    def intrinsic(self, underlying_price: float) -> float:
        if not self.is_option or self.strike is None:
            raise ValueError(f"{self.symbol} is not an option")
        if self.option_type == "CE":
            return max(underlying_price - self.strike, 0.0)
        return max(self.strike - underlying_price, 0.0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        for k in ("expiry", "listed_from", "listed_to"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @staticmethod
    def from_dict(d: dict) -> "Instrument":
        d = dict(d)
        d["kind"] = Kind(d["kind"])
        for k in ("expiry", "listed_from", "listed_to"):
            v = d.get(k)
            d[k] = date.fromisoformat(v) if isinstance(v, str) and v else None
        for k in ("strike", "price_band_pct"):
            v = d.get(k)
            d[k] = None if v is None or v != v else float(v)
        for k in ("underlying", "underlying_kind", "option_type", "sector", "isin"):
            v = d.get(k)
            d[k] = None if v is None or v != v else str(v)
        d["lot_size"] = int(d.get("lot_size") or 1)
        return Instrument(**{k: d[k] for k in Instrument.__dataclass_fields__ if k in d})


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A corporate action effective at the open of `ex_date`.

    kind = "split" / "bonus": holders get `ratio` shares for each share held
           (2.0 for a 1:2 split or a 1:1 bonus). Prices fall by the same factor.
    kind = "dividend": `amount` rupees per share, cash on ex-date (simplified:
           real payment date is later; documented in docs/ARCHITECTURE.md).
    """
    symbol: str
    ex_date: date
    kind: str
    ratio: float = 1.0
    amount: float = 0.0

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "ex_date": self.ex_date.isoformat(), "kind": self.kind,
                "ratio": self.ratio, "amount": self.amount}

    @staticmethod
    def from_dict(d: dict) -> "CorporateAction":
        return CorporateAction(d["symbol"], date.fromisoformat(d["ex_date"]), d["kind"],
                               float(d.get("ratio", 1.0)), float(d.get("amount", 0.0)))


@dataclass(frozen=True, slots=True)
class IndexMembership:
    """Point-in-time index constituent record: `symbol` was in `index` on [start, end]."""
    index: str
    symbol: str
    start: date
    end: date | None = None

    def active_on(self, d: date) -> bool:
        return self.start <= d and (self.end is None or d <= self.end)
