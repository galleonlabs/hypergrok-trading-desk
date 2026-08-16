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
    max_risk_pct: Decimal | None = None,
) -> SizeResult:
    """Size a position from a stop distance.

    HyperGrok imposes no risk-per-trade ceiling. Hyperliquid does not define one
    either -- its real constraints are per-asset max leverage, tiered margin and
    the liquidation engine, which `hypergrok limits <COIN>` reports. Judging an
    appropriate risk budget is the risk officer's job.

    `max_risk_pct` is therefore an optional opt-in guardrail (HYPERGROK_MAX_RISK_PCT)
    for anyone who wants the CLI itself to refuse a fat-fingered figure. None
    means no ceiling; the arithmetic is identical either way.
    """
    values = (equity, entry, stop, risk_pct, max_notional)
    if any(not value.is_finite() for value in values):
        raise RiskError("All sizing inputs must be finite")
    if equity <= 0 or entry <= 0 or stop <= 0 or max_notional <= 0:
        raise RiskError("Equity, prices and cap must be positive")
    if risk_pct <= 0:
        raise RiskError("Risk per trade must be above 0%")
    if max_risk_pct is not None:
        if not max_risk_pct.is_finite() or max_risk_pct <= 0:
            raise RiskError("Configured maximum risk per trade must be positive")
        if risk_pct > max_risk_pct:
            raise RiskError(
                f"Risk per trade {risk_pct}% exceeds your configured ceiling of "
                f"{max_risk_pct}%. Raise or unset HYPERGROK_MAX_RISK_PCT to allow it."
            )
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
