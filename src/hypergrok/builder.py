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
    max_fee_tenths_bp: int | None
    abstraction: str

    @property
    def balance_eligible(self) -> bool:
        return self.account_value >= Decimal("100")

    @property
    def standard_mode(self) -> bool:
        return self.abstraction == "disabled"

    @property
    def approval_sufficient(self) -> bool | None:
        if self.max_fee_tenths_bp is None:
            return None
        return self.max_fee_tenths_bp >= GALLEON_BUILDER_FEE_TENTHS_BP

    @property
    def eligible(self) -> bool:
        return self.balance_eligible and self.standard_mode and self.approval_sufficient is True


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


def inspect_builder(user: str | None, info: Callable[..., Any]) -> BuilderStatus:
    state = info("clearinghouseState", user=GALLEON_BUILDER_ADDRESS)
    try:
        value = Decimal(str(state["marginSummary"]["accountValue"]))
        if not value.is_finite():
            raise ValueError("non-finite account value")
    except Exception as exc:
        raise BuilderError("Could not verify builder perps account value") from exc
    abstraction = str(info("userAbstraction", user=GALLEON_BUILDER_ADDRESS))
    approved = None
    if user is not None:
        approved = _max_fee(info("maxBuilderFee", user=user, builder=GALLEON_BUILDER_ADDRESS))
    return BuilderStatus(
        account_value=value,
        max_fee_tenths_bp=approved,
        abstraction=abstraction,
    )


def check_builder(user: str, info: Callable[..., Any]) -> BuilderStatus:
    status = inspect_builder(user, info)
    if not status.balance_eligible:
        raise BuilderError(
            f"Galleon builder is ineligible: perps account value is {status.account_value} USDC, needs 100"
        )
    if status.approval_sufficient is not True:
        raise BuilderError("User has not approved the 1 bp Galleon builder fee")
    if not status.standard_mode:
        raise BuilderError(
            f"Galleon builder is not in standard mode: userAbstraction={status.abstraction}"
        )
    return status
