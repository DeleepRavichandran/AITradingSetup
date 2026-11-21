"""Money is held as integer paise so that cash accounting is exact.

Prices in market data are floats (rupees). At the moment a price becomes a cash
flow (a fill, a settlement, a charge) it is converted to integer paise. Every
ledger invariant in the backtester is therefore an exact integer equality, not a
floating point comparison with a tolerance.
"""
from __future__ import annotations

import math

PAISE = 100


def to_paise(rupees: float) -> int:
    """Round-half-up conversion of a rupee amount to integer paise."""
    return int(math.floor(rupees * PAISE + 0.5))


def to_rupees(paise: int) -> float:
    return paise / PAISE


def round_to_tick(price: float, tick: float, direction: int = 0) -> float:
    """Round a price to the tick grid.

    direction > 0 rounds up (adverse for a buyer), < 0 rounds down (adverse for a
    seller), 0 rounds to nearest.
    """
    if tick <= 0:
        return price
    n = price / tick
    eps = 1e-9
    if direction > 0:
        k = math.ceil(n - eps)
    elif direction < 0:
        k = math.floor(n + eps)
    else:
        k = math.floor(n + 0.5)
    return round(k * tick, 6)


def fmt_inr(paise: int) -> str:
    return f"₹{paise / PAISE:,.2f}"
