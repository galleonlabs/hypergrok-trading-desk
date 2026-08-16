"""Hyperliquid builder eligibility and approval checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import GALLEON_BUILDER_ADDRESS, GALLEON_BUILDER_FEE_TENTHS_BP


class BuilderError(RuntimeError):
    """Builder attribution is not eligible or not approved."""


@dataclass(frozen=True)
class BuilderStatus:
    account_value: Decimal
    max_fee_tenths_bp: int
    abstraction: str

    @property
    def eligible(self) -> bool:
        return (
            self.account_value >= Decimal("100")
            and self.max_fee_tenths_bp >= GALLEON_BUILDER_FEE_TENTHS_BP
            and self.abstraction == "disabled"
        )


def _max_fee(raw: Any) -> int:
    if isinstance(raw, bool):
        raise BuilderError("Unexpected maxBuilderFee response")
    if isinstance(raw, (int, str)):
        return int(raw)
    if isinstance(raw, dict):
        for key in ("maxBuilderFee", "maxFee", "fee"):
            if key in raw:
                return int(raw[key])
    raise BuilderError("Unexpected maxBuilderFee response")


def check_builder(user: str, info: Callable[..., Any]) -> BuilderStatus:
    state = info("clearinghouseState", user=GALLEON_BUILDER_ADDRESS)
    try:
        value = Decimal(str(state["marginSummary"]["accountValue"]))
    except Exception as exc:
        raise BuilderError("Could not verify builder perps account value") from exc
    approved = info("maxBuilderFee", user=user, builder=GALLEON_BUILDER_ADDRESS)
    abstraction = str(info("userAbstraction", user=GALLEON_BUILDER_ADDRESS))
    status = BuilderStatus(
        account_value=value,
        max_fee_tenths_bp=_max_fee(approved),
        abstraction=abstraction,
    )
    if value < Decimal("100"):
        raise BuilderError(f"Galleon builder is ineligible: perps account value is {value} USDC, needs 100")
    if status.max_fee_tenths_bp < GALLEON_BUILDER_FEE_TENTHS_BP:
        raise BuilderError("User has not approved the disclosed 1 bp Galleon builder fee")
    if abstraction != "disabled":
        raise BuilderError(
            f"Galleon builder is not in standard mode: userAbstraction={abstraction}"
        )
    return status
