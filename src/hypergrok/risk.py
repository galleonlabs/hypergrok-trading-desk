"""Deterministic pre-trade sizing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


class RiskError(ValueError):
    """A proposed trade violates a deterministic risk rule."""


@dataclass(frozen=True)
class SizeResult:
    size: Decimal
    notional: Decimal
    risk_usd: Decimal
    risk_pct: Decimal


def size_for_stop(
    *,
    equity: Decimal,
    entry: Decimal,
    stop: Decimal,
    risk_pct: Decimal,
    max_notional: Decimal,
) -> SizeResult:
    values = (equity, entry, stop, risk_pct, max_notional)
    if any(not value.is_finite() for value in values):
        raise RiskError("All sizing inputs must be finite")
    if equity <= 0 or entry <= 0 or stop <= 0 or max_notional <= 0:
        raise RiskError("Equity, prices and cap must be positive")
    if not Decimal("0") < risk_pct <= Decimal("2"):
        raise RiskError("Risk per trade must be above 0% and at most 2%")
    distance = abs(entry - stop)
    if distance == 0:
        raise RiskError("Entry and stop cannot be equal")
    risk_budget = equity * risk_pct / Decimal("100")
    risk_size = risk_budget / distance
    cap_size = max_notional / entry
    size = min(risk_size, cap_size).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if size <= 0:
        raise RiskError("Calculated size is zero")
    notional = size * entry
    actual_risk = size * distance
    return SizeResult(size=size, notional=notional, risk_usd=actual_risk, risk_pct=actual_risk / equity * 100)
