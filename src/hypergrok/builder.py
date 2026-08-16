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


@dataclass(frozen=True)
class Attribution:
    """Whether this order carries the Galleon builder fee, and why."""

    active: bool
    reason: str

    @property
    def payload(self) -> dict[str, Any] | None:
        if not self.active:
            return None
        return {"b": GALLEON_BUILDER_ADDRESS, "f": GALLEON_BUILDER_FEE_TENTHS_BP}


def resolve_attribution(network: str, user: str, info: Callable[..., Any]) -> Attribution:
    """Decide builder attribution without ever blocking the user's order.

    Builder codes are a mainnet fee-attribution mechanism. Whether Galleon can
    currently collect that fee is Galleon's operational concern, not a reason to
    refuse someone else's trade, so every failure path here degrades to an
    unattributed order rather than raising. The user's own safety gates -- plan
    hash, account match, API-wallet role, price drift, precision, duplicate
    cloid and the local journal -- are enforced separately and are not affected.
    """
    if network != "mainnet":
        return Attribution(False, "Builder attribution applies on mainnet only.")
    try:
        status = inspect_builder(user, info)
    except Exception as exc:  # noqa: BLE001 - attribution must never block a trade
        return Attribution(False, f"Builder status could not be read ({type(exc).__name__}).")
    if not status.balance_eligible:
        return Attribution(False, "Galleon builder is below the 100 USDC requirement.")
    if not status.standard_mode:
        return Attribution(False, "Galleon builder is not in standard mode.")
    if status.approval_sufficient is not True:
        return Attribution(
            False,
            "You have not approved the 1 bp builder fee. Approve it to support HyperGrok.",
        )
    return Attribution(True, "1 bp builder fee attributed to Galleon.")
