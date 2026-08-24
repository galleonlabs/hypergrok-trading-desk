"""Exact, allowlisted Hyperliquid order-wire construction.

This module deliberately does not sign or transmit anything.  It translates a
validated :class:`ProtectedTradePlan` into the compact Hyperliquid order action
while rejecting every value that would require venue-side or float rounding.
The signer boundary can therefore accept this one typed action instead of an
arbitrary exchange payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .domain import Environment, SemanticIntent, Side
from .errors import ValidationError
from .planning import GroupingPolicy, ProtectedTradePlan


_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_ZERO = Decimal("0")
_MIN_PERP_NOTIONAL = Decimal("10")


class HyperliquidNetwork(str, Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"

    @property
    def environment(self) -> Environment:
        return (
            Environment.MAINNET
            if self is HyperliquidNetwork.MAINNET
            else Environment.TESTNET
        )

    @property
    def exchange_url(self) -> str:
        return (
            "https://api.hyperliquid.xyz/exchange"
            if self is HyperliquidNetwork.MAINNET
            else "https://api.hyperliquid-testnet.xyz/exchange"
        )


@dataclass(frozen=True, slots=True)
class PerpInstrumentMetadata:
    """Fresh metadata needed to encode one default-dex perpetual."""

    symbol: str
    asset_id: int
    sz_decimals: int
    max_leverage: Decimal
    margin_mode: str
    is_delisted: bool
    source_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValidationError("metadata symbol is invalid")
        if type(self.asset_id) is not int or not 0 <= self.asset_id <= 1_000_000:
            raise ValidationError("asset_id must be a bounded non-negative integer")
        if type(self.sz_decimals) is not int or not 0 <= self.sz_decimals <= 8:
            raise ValidationError("sz_decimals must be an integer from 0 to 8")
        if not isinstance(self.max_leverage, Decimal):
            raise TypeError("max_leverage must be Decimal")
        validate_decimal_bounds(self.max_leverage, field="max_leverage")
        if self.max_leverage <= _ZERO:
            raise ValidationError("max_leverage must be positive")
        if (
            not isinstance(self.margin_mode, str)
            or self.margin_mode not in {"cross", "isolated", "strictIsolated", "noCross"}
        ):
            raise ValidationError("unsupported perpetual margin mode")
        if type(self.is_delisted) is not bool:
            raise TypeError("is_delisted must be bool")
        if (
            not isinstance(self.source_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_hash)
        ):
            raise ValidationError("source_hash must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ProtectedOrderAction:
    """A plan-bound, unsigned Hyperliquid action and its canonical digest."""

    network: HyperliquidNetwork
    account_id: str
    plan_hash: str
    metadata_hash: str
    expires_at_ms: int
    action: dict[str, Any]
    action_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hyperliquid.protected_order_action.v1",
            "network": self.network.value,
            "account_id": self.account_id,
            "plan_hash": self.plan_hash,
            "metadata_hash": self.metadata_hash,
            "expires_at_ms": self.expires_at_ms,
            "action": self.action,
            "action_hash": self.action_hash,
            "signed": False,
            "submitted": False,
        }


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _fractional_places(value: Decimal) -> int:
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _significant_digits(value: Decimal) -> int:
    normalized = value.normalize()
    digits = normalized.as_tuple().digits
    return len(digits) if any(digits) else 1


def format_perp_size(value: Decimal, *, sz_decimals: int) -> str:
    """Validate and canonically encode a perp size without rounding."""

    if not isinstance(value, Decimal):
        raise TypeError("size must be Decimal")
    validate_decimal_bounds(value, field="size")
    if value <= _ZERO:
        raise ValidationError("size must be positive")
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 8:
        raise ValidationError("sz_decimals must be an integer from 0 to 8")
    if _fractional_places(value) > sz_decimals:
        raise ValidationError("size exceeds Hyperliquid szDecimals")
    return canonical_decimal(value)


def format_perp_price(value: Decimal, *, sz_decimals: int) -> str:
    """Validate Hyperliquid's perp tick rule and encode without rounding."""

    if not isinstance(value, Decimal):
        raise TypeError("price must be Decimal")
    validate_decimal_bounds(value, field="price")
    if value <= _ZERO:
        raise ValidationError("price must be positive")
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 6:
        raise ValidationError("perp sz_decimals must be an integer from 0 to 6")
    text = canonical_decimal(value)
    if "." not in text:
        # Hyperliquid explicitly permits integer prices regardless of their
        # significant-figure count.
        return text
    if _fractional_places(value) > 6 - sz_decimals:
        raise ValidationError("price exceeds Hyperliquid perp decimal places")
    if _significant_digits(value) > 5:
        raise ValidationError("non-integer price exceeds five significant figures")
    return text


def _limit_wire(
    intent: SemanticIntent,
    *,
    asset_id: int,
    sz_decimals: int,
) -> dict[str, Any]:
    if intent.price_bound is None:
        raise ValidationError("bounded entry price is required")
    if intent.time_in_force != "Ioc":
        raise ValidationError("initial protected entries require exact Ioc time in force")
    if not _CLOID_RE.fullmatch(intent.client_order_id):
        raise ValidationError("client order ID is not a 128-bit lowercase hex CLOID")
    return {
        "a": asset_id,
        "b": intent.side is Side.BUY,
        "p": format_perp_price(intent.price_bound, sz_decimals=sz_decimals),
        "s": format_perp_size(intent.quantity, sz_decimals=sz_decimals),
        "r": False,
        "t": {"limit": {"tif": "Ioc"}},
        "c": intent.client_order_id,
    }


def _trigger_wire(
    intent: SemanticIntent,
    *,
    asset_id: int,
    sz_decimals: int,
    trigger_kind: str,
) -> dict[str, Any]:
    if trigger_kind not in {"sl", "tp"}:
        raise ValidationError("unsupported trigger kind")
    if not intent.reduce_only or intent.stop_price is None or intent.price_bound is None:
        raise ValidationError("protected trigger must be reduce-only and price bounded")
    if not _CLOID_RE.fullmatch(intent.client_order_id):
        raise ValidationError("client order ID is not a 128-bit lowercase hex CLOID")
    limit_price = (
        intent.protection_limit_price
        if trigger_kind == "sl" and intent.protection_limit_price is not None
        else intent.price_bound
    )
    return {
        "a": asset_id,
        "b": intent.side is Side.BUY,
        "p": format_perp_price(limit_price, sz_decimals=sz_decimals),
        "s": format_perp_size(intent.quantity, sz_decimals=sz_decimals),
        "r": True,
        "t": {
            "trigger": {
                "isMarket": True,
                "triggerPx": format_perp_price(
                    intent.stop_price,
                    sz_decimals=sz_decimals,
                ),
                "tpsl": trigger_kind,
            }
        },
        "c": intent.client_order_id,
    }


def build_protected_order_action(
    plan: ProtectedTradePlan,
    metadata: PerpInstrumentMetadata,
    *,
    network: HyperliquidNetwork,
    at: datetime,
) -> ProtectedOrderAction:
    """Translate one still-live protected plan into the only allowed entry action."""

    if not isinstance(plan, ProtectedTradePlan):
        raise TypeError("plan must be ProtectedTradePlan")
    if not isinstance(metadata, PerpInstrumentMetadata):
        raise TypeError("metadata must be PerpInstrumentMetadata")
    if not isinstance(network, HyperliquidNetwork):
        try:
            network = HyperliquidNetwork(network)
        except (TypeError, ValueError) as error:
            raise ValidationError("network must be explicit mainnet or testnet") from error
    checked_at = _utc(at, "at")
    entry = plan.entry
    if entry.venue != "hyperliquid":
        raise ValidationError("protected plan venue must be hyperliquid")
    if entry.environment is not network.environment:
        raise ValidationError("plan environment does not match explicit network")
    if entry.instrument not in {metadata.symbol, f"{metadata.symbol}-PERP"}:
        raise ValidationError("fresh metadata does not match plan instrument")
    if metadata.is_delisted:
        raise ValidationError("delisted instruments cannot enter new risk")
    if entry.leverage is None or entry.leverage > metadata.max_leverage:
        raise ValidationError("plan leverage exceeds fresh metadata")
    if checked_at >= entry.expires_at:
        raise ValidationError("protected plan has expired")
    expires_at_ms = int(entry.expires_at.timestamp() * 1000)
    entry_price = entry.price_bound
    if entry_price is None:
        raise ValidationError("protected entry lacks a price bound")
    if entry.quantity * entry_price < _MIN_PERP_NOTIONAL:
        raise ValidationError("protected entry is below the Hyperliquid perp minimum")
    if plan.grouping is not GroupingPolicy.NORMAL_TPSL:
        raise ValidationError("protected entry requires normalTpsl")

    action = {
        "type": "order",
        "orders": [
            _limit_wire(
                entry,
                asset_id=metadata.asset_id,
                sz_decimals=metadata.sz_decimals,
            ),
            _trigger_wire(
                plan.protective_stop,
                asset_id=metadata.asset_id,
                sz_decimals=metadata.sz_decimals,
                trigger_kind="sl",
            ),
            _trigger_wire(
                plan.take_profit,
                asset_id=metadata.asset_id,
                sz_decimals=metadata.sz_decimals,
                trigger_kind="tp",
            ),
        ],
        "grouping": "normalTpsl",
    }
    binding = {
        "network": network.value,
        "account_id": entry.account_id,
        "plan_hash": plan.plan_hash,
        "metadata_hash": metadata.source_hash,
        "expires_at_ms": expires_at_ms,
        "action": action,
    }
    return ProtectedOrderAction(
        network=network,
        account_id=entry.account_id,
        plan_hash=plan.plan_hash,
        metadata_hash=metadata.source_hash,
        expires_at_ms=expires_at_ms,
        action=action,
        action_hash=domain_hash("trading-harness/hyperliquid-action/v1", binding),
    )


__all__ = (
    "HyperliquidNetwork",
    "PerpInstrumentMetadata",
    "ProtectedOrderAction",
    "build_protected_order_action",
    "format_perp_price",
    "format_perp_size",
)
