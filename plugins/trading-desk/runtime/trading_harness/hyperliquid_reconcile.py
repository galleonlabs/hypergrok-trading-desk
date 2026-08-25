"""Read-only Hyperliquid venue reconciliation into ExecutionStore inputs.

Every owned leg is queried by its exact CLOID.  User fills are read with
``aggregateByTime=false`` using inclusive timestamp pagination, deduplicated by
venue identity, and converted without consulting the display-only ``dir``
field.  The resulting bundle contains the exact ``LegReconciliation`` and
``VenueFill`` values accepted by :class:`ExecutionStore`, plus signed fill,
position, and top-level stop-coverage evidence.

The API exposes only 2,000 fills per page and the latest 10,000 fills.  Page or
retention saturation is never silently upgraded to complete reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, localcontext
from enum import Enum
import hashlib
import json
import re
from typing import TypeAlias

from .canonical import canonical_decimal, canonical_json, domain_hash, validate_decimal_bounds
from .errors import HarnessError, RecordNotFound, StateConflict, ValidationError
from .execution_store import (
    ExecutionStore,
    LegReconciliation,
    RecoveryVenueFill,
    VenueFill,
)
from .hyperliquid_account import (
    HyperliquidAccountSnapshot,
    OrderSide,
)
from .hyperliquid_wire import HyperliquidNetwork
from .hyperliquid_recovery import (
    ReduceOnlyCloseAction,
    recovery_action_from_material,
)
from .market_data import post_public_info, public_info_endpoint


InfoTransport: TypeAlias = Callable[[str, Mapping[str, object]], object]
Clock: TypeAlias = Callable[[], datetime]

VENUE_RECONCILIATION_HASH_DOMAIN = (
    "trading-harness/hyperliquid-venue-reconciliation/v1"
)
USER_FILLS_PAGE_LIMIT = 2_000
USER_FILLS_RETENTION_LIMIT = 10_000
FILL_LOOKBACK_MS = 5_000

_ROLES = ("entry", "protective_stop", "take_profit")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
_ZERO = Decimal("0")
_EXACT_CONTEXT = Context(prec=256)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MAX_SNAPSHOT_AGE_MS = 5_000
_MAX_FUTURE_SKEW_MS = 5_000
_MAX_FILL_PAGES = 6
_LATE_WRITE_SETTLEMENT_MS = 5_000

_ORDER_STATUSES = frozenset(
    {
        "open",
        "filled",
        "canceled",
        "triggered",
        "rejected",
        "marginCanceled",
        "vaultWithdrawalCanceled",
        "openInterestCapCanceled",
        "selfTradeCanceled",
        "reduceOnlyCanceled",
        "siblingFilledCanceled",
        "delistedCanceled",
        "liquidatedCanceled",
        "scheduledCancel",
        "tickRejected",
        "minTradeNtlRejected",
        "perpMarginRejected",
        "reduceOnlyRejected",
        "badAloPxRejected",
        "iocCancelRejected",
        "badTriggerPxRejected",
        "marketOrderNoLiquidityRejected",
        "positionIncreaseAtOpenInterestCapRejected",
        "positionFlipAtOpenInterestCapRejected",
        "tooAggressiveAtOpenInterestCapRejected",
        "openInterestIncreaseRejected",
        "insufficientSpotBalanceRejected",
        "oracleRejected",
        "perpMaxPositionRejected",
    }
)
_CANCELED_STATUSES = frozenset(
    {
        "canceled",
        "marginCanceled",
        "vaultWithdrawalCanceled",
        "openInterestCapCanceled",
        "selfTradeCanceled",
        "reduceOnlyCanceled",
        "siblingFilledCanceled",
        "delistedCanceled",
        "liquidatedCanceled",
        "scheduledCancel",
    }
)
_REJECTED_STATUSES = frozenset(
    status for status in _ORDER_STATUSES if status == "rejected" or status.endswith("Rejected")
)
_AUXILIARY_TERMINAL_STATUSES = frozenset(
    {"filled", *_CANCELED_STATUSES, *_REJECTED_STATUSES}
)


class HyperliquidReconcileError(HarnessError):
    """Base class for expected read-only reconciliation failures."""


class HyperliquidReconcileTransportError(HyperliquidReconcileError):
    """The allowlisted info endpoint could not be read."""


class HyperliquidReconcileResponseError(HyperliquidReconcileError, ValueError):
    """Venue reconciliation data violated its current documented schema."""


class VenueOrderState(str, Enum):
    ORDER = "order"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class OwnedLeg:
    role: str
    cloid: str
    symbol: str
    side: OrderSide
    requested_quantity: Decimal

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValidationError("owned leg role is invalid")
        if not isinstance(self.cloid, str) or not _CLOID_RE.fullmatch(self.cloid):
            raise ValidationError("owned leg CLOID must be lowercase 128-bit hex")
        if not isinstance(self.symbol, str) or not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValidationError("owned leg symbol is invalid")
        if not isinstance(self.side, OrderSide):
            try:
                object.__setattr__(self, "side", OrderSide(self.side))
            except (TypeError, ValueError) as error:
                raise ValidationError("owned leg side is invalid") from error
        if not isinstance(self.requested_quantity, Decimal):
            raise TypeError("owned leg requested_quantity must be Decimal")
        validate_decimal_bounds(self.requested_quantity, field="requested_quantity")
        if self.requested_quantity <= _ZERO:
            raise ValidationError("owned leg requested_quantity must be positive")

    def canonical_record(self) -> dict[str, object]:
        return {
            "role": self.role,
            "cloid": self.cloid,
            "symbol": self.symbol,
            "side": self.side.value,
            "requested_quantity": canonical_decimal(self.requested_quantity),
        }


@dataclass(frozen=True, slots=True)
class ParsedOrderStatus:
    role: str
    requested_cloid: str
    state: VenueOrderState
    venue_status: str | None
    status_timestamp_ms: int | None
    oid: int | None
    symbol: str | None
    remaining_size: Decimal | None
    original_size: Decimal | None
    is_trigger: bool | None
    reduce_only: bool | None

    def canonical_record(self) -> dict[str, object]:
        return {
            "role": self.role,
            "requested_cloid": self.requested_cloid,
            "state": self.state.value,
            "venue_status": self.venue_status,
            "status_timestamp_ms": self.status_timestamp_ms,
            "oid": self.oid,
            "symbol": self.symbol,
            "remaining_size": (
                None
                if self.remaining_size is None
                else canonical_decimal(self.remaining_size)
            ),
            "original_size": (
                None
                if self.original_size is None
                else canonical_decimal(self.original_size)
            ),
            "is_trigger": self.is_trigger,
            "reduce_only": self.reduce_only,
        }


@dataclass(frozen=True, slots=True)
class SignedFillEvidence:
    fill_id: str
    role: str
    cloid: str
    oid: int
    tid: int
    transaction_hash: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    signed_quantity: Decimal
    start_position: Decimal
    end_position: Decimal
    price: Decimal
    fee: Decimal
    closed_pnl: Decimal
    fee_token: str
    crossed: bool
    builder_fee: Decimal | None
    time_ms: int

    def canonical_record(self) -> dict[str, object]:
        return {
            "fill_id": self.fill_id,
            "role": self.role,
            "cloid": self.cloid,
            "oid": self.oid,
            "tid": self.tid,
            "transaction_hash": self.transaction_hash,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": canonical_decimal(self.quantity),
            "signed_quantity": canonical_decimal(self.signed_quantity),
            "start_position": canonical_decimal(self.start_position),
            "end_position": canonical_decimal(self.end_position),
            "price": canonical_decimal(self.price),
            "fee": canonical_decimal(self.fee),
            "closed_pnl": canonical_decimal(self.closed_pnl),
            "fee_token": self.fee_token,
            "crossed": self.crossed,
            "builder_fee": (
                None
                if self.builder_fee is None
                else canonical_decimal(self.builder_fee)
            ),
            "time_ms": self.time_ms,
        }


def canonical_hyperliquid_fill_id(fill: SignedFillEvidence) -> str:
    if not isinstance(fill, SignedFillEvidence):
        raise TypeError("fill must be SignedFillEvidence")
    return f"hyperliquid:{fill.symbol}:{fill.time_ms}:{fill.tid}:{fill.oid}"


@dataclass(frozen=True, slots=True)
class AuxiliaryOwnedOrder:
    """A non-target order proven to belong to this parent lifecycle."""

    owner_kind: str
    owner_id: str
    source_hash: str
    role: str
    cloid: str
    symbol: str
    side: OrderSide
    requested_quantity: Decimal
    is_trigger: bool
    reduce_only: bool
    expires_after_ms: int | None = None

    def __post_init__(self) -> None:
        if self.owner_kind not in {"parent_leg", "recovery_close"}:
            raise ValidationError("auxiliary order owner kind is unsupported")
        _input_text(self.owner_id, "auxiliary owner_id", maximum=128)
        _input_text(self.role, "auxiliary role", maximum=64)
        if not isinstance(self.source_hash, str) or not _HASH_RE.fullmatch(
            self.source_hash
        ):
            raise ValidationError("auxiliary order source hash is invalid")
        if not isinstance(self.cloid, str) or not _CLOID_RE.fullmatch(self.cloid):
            raise ValidationError("auxiliary order CLOID is invalid")
        if not isinstance(self.symbol, str) or not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValidationError("auxiliary order symbol is invalid")
        if not isinstance(self.side, OrderSide):
            raise TypeError("auxiliary order side must be OrderSide")
        if not isinstance(self.requested_quantity, Decimal):
            raise TypeError("auxiliary order quantity must be Decimal")
        validate_decimal_bounds(
            self.requested_quantity, field="auxiliary requested_quantity"
        )
        if self.requested_quantity <= _ZERO:
            raise ValidationError("auxiliary order quantity must be positive")
        if type(self.is_trigger) is not bool or type(self.reduce_only) is not bool:
            raise TypeError("auxiliary order flags must be boolean")
        if self.expires_after_ms is not None and (
            type(self.expires_after_ms) is not int or self.expires_after_ms < 0
        ):
            raise ValidationError("auxiliary order expiry is invalid")

    def canonical_record(self) -> dict[str, object]:
        return {
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "source_hash": self.source_hash,
            "role": self.role,
            "cloid": self.cloid,
            "symbol": self.symbol,
            "side": self.side.value,
            "requested_quantity": canonical_decimal(self.requested_quantity),
            "is_trigger": self.is_trigger,
            "reduce_only": self.reduce_only,
            "expires_after_ms": self.expires_after_ms,
        }


@dataclass(frozen=True, slots=True)
class AuxiliaryOrderEvidence:
    order: AuxiliaryOwnedOrder
    status: ParsedOrderStatus

    def __post_init__(self) -> None:
        if not isinstance(self.order, AuxiliaryOwnedOrder):
            raise TypeError("auxiliary order evidence requires an owned order")
        if not isinstance(self.status, ParsedOrderStatus):
            raise TypeError("auxiliary order evidence requires a parsed status")
        if self.status.requested_cloid != self.order.cloid:
            raise ValidationError("auxiliary status CLOID differs from its owner")

    def canonical_record(self) -> dict[str, object]:
        return {
            "order": self.order.canonical_record(),
            "status": self.status.canonical_record(),
        }


@dataclass(frozen=True, slots=True)
class AuxiliaryFillEvidence:
    owner_kind: str
    owner_id: str
    source_hash: str
    fill: SignedFillEvidence

    def __post_init__(self) -> None:
        if self.owner_kind not in {"parent_leg", "recovery_close"}:
            raise ValidationError("auxiliary fill owner kind is unsupported")
        _input_text(self.owner_id, "auxiliary fill owner_id", maximum=128)
        if not isinstance(self.source_hash, str) or not _HASH_RE.fullmatch(
            self.source_hash
        ):
            raise ValidationError("auxiliary fill source hash is invalid")
        if not isinstance(self.fill, SignedFillEvidence):
            raise TypeError("auxiliary fill evidence requires signed fill evidence")

    def canonical_record(self) -> dict[str, object]:
        return {
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "source_hash": self.source_hash,
            "fill": self.fill.canonical_record(),
        }


@dataclass(frozen=True, slots=True)
class FillCoverage:
    requested_start_time_ms: int
    requested_end_time_ms: int
    page_count: int
    page_limit: int
    retention_limit: int
    returned_rows: int
    unique_fills: int
    duplicate_fills: int
    unmatched_fills: int
    page_saturated: bool
    retention_limited: bool
    complete: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_start_time_ms": self.requested_start_time_ms,
            "requested_end_time_ms": self.requested_end_time_ms,
            "page_count": self.page_count,
            "page_limit": self.page_limit,
            "retention_limit": self.retention_limit,
            "returned_rows": self.returned_rows,
            "unique_fills": self.unique_fills,
            "duplicate_fills": self.duplicate_fills,
            "unmatched_fills": self.unmatched_fills,
            "page_saturated": self.page_saturated,
            "retention_limited": self.retention_limited,
            "complete": self.complete,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class VenueReconciliationBundle:
    network: HyperliquidNetwork
    main_account_address: str
    account_id: str
    command_id: str
    plan_hash: str
    account_snapshot_hash: str
    observed_at: datetime
    order_statuses: tuple[ParsedOrderStatus, ...]
    signed_fills: tuple[SignedFillEvidence, ...]
    fill_coverage: FillCoverage
    legs: tuple[LegReconciliation, ...]
    fills: tuple[VenueFill, ...]
    signed_position_quantity: Decimal
    protected_quantity: Decimal
    complete: bool
    incomplete_reasons: tuple[str, ...]
    reconciliation_hash: str
    auxiliary_order_statuses: tuple[AuxiliaryOrderEvidence, ...] = ()
    auxiliary_fills: tuple[AuxiliaryFillEvidence, ...] = ()

    def execution_store_kwargs(self) -> dict[str, object]:
        """Return exactly the venue evidence fields consumed by ``reconcile``."""

        return {
            "account_snapshot_hash": self.account_snapshot_hash,
            "observed_at": self.observed_at,
            "complete": self.complete,
            "legs": self.legs,
            "signed_position_quantity": self.signed_position_quantity,
            "protected_quantity": self.protected_quantity,
            "fills": self.fills,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "hyperliquid.venue_reconciliation.v1",
            "network": self.network.value,
            "main_account_address": self.main_account_address,
            "account_id": self.account_id,
            "command_id": self.command_id,
            "plan_hash": self.plan_hash,
            "account_snapshot_hash": self.account_snapshot_hash,
            "observed_at": self.observed_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "order_statuses": [item.canonical_record() for item in self.order_statuses],
            "signed_fills": [item.canonical_record() for item in self.signed_fills],
            "auxiliary_order_statuses": [
                item.canonical_record() for item in self.auxiliary_order_statuses
            ],
            "auxiliary_fills": [
                item.canonical_record() for item in self.auxiliary_fills
            ],
            "fill_coverage": self.fill_coverage.as_dict(),
            "legs": [item.as_dict() for item in self.legs],
            "fills": [item.as_dict() for item in self.fills],
            "signed_position_quantity": canonical_decimal(
                self.signed_position_quantity
            ),
            "protected_quantity": canonical_decimal(self.protected_quantity),
            "complete": self.complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "reconciliation_hash": self.reconciliation_hash,
            "read_only": True,
        }


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HyperliquidReconcileResponseError(f"{field} must be a JSON object")
    return value


def _array(value: object, field: str, *, maximum: int | None = None) -> list[object]:
    if not isinstance(value, list):
        raise HyperliquidReconcileResponseError(f"{field} must be a JSON array")
    if maximum is not None and len(value) > maximum:
        raise HyperliquidReconcileResponseError(
            f"{field} exceeds the documented limit of {maximum}"
        )
    return value


def _integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_TIMESTAMP_MS,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HyperliquidReconcileResponseError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise HyperliquidReconcileResponseError(f"{field} is outside supported bounds")
    return value


def _request_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer millisecond timestamp")
    if not 0 <= value <= _MAX_TIMESTAMP_MS:
        raise ValidationError(f"{field} is outside supported bounds")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise HyperliquidReconcileResponseError(f"{field} must be boolean")
    return value


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise HyperliquidReconcileResponseError(f"{field} is invalid")
    return value


def _input_text(value: object, field: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} is invalid")
    return value


def _exact_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HyperliquidReconcileResponseError(f"{field} must be an exact decimal string")
    try:
        result = Decimal(value)
        validate_decimal_bounds(result, field=field)
    except (DecimalException, ValueError) as error:
        raise HyperliquidReconcileResponseError(
            f"{field} must be a bounded finite decimal"
        ) from error
    if positive and result <= _ZERO:
        raise HyperliquidReconcileResponseError(f"{field} must be positive")
    if nonnegative and result < _ZERO:
        raise HyperliquidReconcileResponseError(f"{field} must be non-negative")
    return result


def _clock_ms(clock: Clock) -> int:
    try:
        value = clock()
    except Exception as error:
        raise ValidationError(f"reconciliation clock failed: {type(error).__name__}") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("reconciliation clock must return timezone-aware datetime")
    utc = value.astimezone(timezone.utc)
    delta = utc - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    return _request_integer(result, "clock time")


def _datetime_ms(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


def _post_info(
    endpoint: str,
    payload: Mapping[str, object],
    transport: InfoTransport,
) -> object:
    try:
        return transport(endpoint, payload)
    except HyperliquidReconcileError:
        raise
    except Exception as error:
        raise HyperliquidReconcileTransportError(
            f"venue reconciliation transport failed: {type(error).__name__}"
        ) from error


_ORDER_FIELDS = {
    "coin",
    "side",
    "limitPx",
    "sz",
    "oid",
    "timestamp",
    "triggerCondition",
    "isTrigger",
    "triggerPx",
    "children",
    "isPositionTpsl",
    "reduceOnly",
    "orderType",
    "origSz",
    "tif",
    "cloid",
}


def _parse_order_status(
    response: object,
    leg: OwnedLeg,
    *,
    now_ms: int,
) -> ParsedOrderStatus:
    root = _mapping(response, f"orderStatus[{leg.role}]")
    if root == {"status": "unknownOid"}:
        return ParsedOrderStatus(
            role=leg.role,
            requested_cloid=leg.cloid,
            state=VenueOrderState.MISSING,
            venue_status=None,
            status_timestamp_ms=None,
            oid=None,
            symbol=None,
            remaining_size=None,
            original_size=None,
            is_trigger=None,
            reduce_only=None,
        )
    if set(root) != {"status", "order"} or root["status"] != "order":
        raise HyperliquidReconcileResponseError("orderStatus root is unsupported")
    outer_order = _mapping(root["order"], f"orderStatus[{leg.role}].order")
    if set(outer_order) != {"order", "status", "statusTimestamp"}:
        raise HyperliquidReconcileResponseError("orderStatus record fields are unsupported")
    venue_status = outer_order["status"]
    if not isinstance(venue_status, str) or venue_status not in _ORDER_STATUSES:
        raise HyperliquidReconcileResponseError("orderStatus venue status is unknown")
    status_time = _integer(
        outer_order["statusTimestamp"],
        f"orderStatus[{leg.role}].statusTimestamp",
    )
    if status_time > now_ms:
        raise HyperliquidReconcileResponseError("order status timestamp is in the future")
    order = _mapping(outer_order["order"], f"orderStatus[{leg.role}].order.order")
    if set(order) != _ORDER_FIELDS:
        raise HyperliquidReconcileResponseError("orderStatus order fields are unsupported")
    if order["cloid"] != leg.cloid:
        raise HyperliquidReconcileResponseError("orderStatus returned a foreign CLOID")
    if order["coin"] != leg.symbol:
        raise HyperliquidReconcileResponseError("orderStatus returned a foreign symbol")
    side_wire = order["side"]
    if side_wire != leg.side.wire_value:
        raise HyperliquidReconcileResponseError("orderStatus side differs from owned leg")
    remaining = _exact_decimal(order["sz"], "orderStatus.sz", nonnegative=True)
    original = _exact_decimal(order["origSz"], "orderStatus.origSz", nonnegative=True)
    position_tpsl = _bool(order["isPositionTpsl"], "orderStatus.isPositionTpsl")
    if original == _ZERO and not position_tpsl:
        raise HyperliquidReconcileResponseError("non-position order has zero original size")
    if original != leg.requested_quantity:
        raise HyperliquidReconcileResponseError("orderStatus original size differs from plan")
    if remaining > original:
        raise HyperliquidReconcileResponseError("orderStatus remaining size exceeds original")
    is_trigger = _bool(order["isTrigger"], "orderStatus.isTrigger")
    reduce_only = _bool(order["reduceOnly"], "orderStatus.reduceOnly")
    expected_trigger = leg.role != "entry"
    if is_trigger != expected_trigger or reduce_only != expected_trigger:
        raise HyperliquidReconcileResponseError("orderStatus role flags differ from plan")
    _exact_decimal(order["limitPx"], "orderStatus.limitPx", positive=True)
    trigger_price = _exact_decimal(
        order["triggerPx"], "orderStatus.triggerPx", nonnegative=True
    )
    if is_trigger != (trigger_price > _ZERO):
        raise HyperliquidReconcileResponseError("orderStatus trigger fields disagree")
    _integer(order["oid"], "orderStatus.oid", maximum=2**63 - 1)
    order_time = _integer(order["timestamp"], "orderStatus.timestamp")
    if order_time > status_time:
        raise HyperliquidReconcileResponseError("order timestamp postdates status")
    _text(order["triggerCondition"], "orderStatus.triggerCondition")
    _text(order["orderType"], "orderStatus.orderType")
    if order["tif"] is not None:
        _text(order["tif"], "orderStatus.tif", maximum=64)
    _array(order["children"], "orderStatus.children", maximum=20)
    return ParsedOrderStatus(
        role=leg.role,
        requested_cloid=leg.cloid,
        state=VenueOrderState.ORDER,
        venue_status=venue_status,
        status_timestamp_ms=status_time,
        oid=order["oid"],  # type: ignore[arg-type]
        symbol=leg.symbol,
        remaining_size=remaining,
        original_size=original,
        is_trigger=is_trigger,
        reduce_only=reduce_only,
    )


def _parse_auxiliary_order_status(
    response: object,
    owned: AuxiliaryOwnedOrder,
    *,
    now_ms: int,
) -> AuxiliaryOrderEvidence:
    """Parse one exact durable auxiliary order without weakening role flags."""

    role = f"auxiliary:{owned.owner_id}:{owned.role}"
    root = _mapping(response, f"orderStatus[{role}]")
    if root == {"status": "unknownOid"}:
        return AuxiliaryOrderEvidence(
            owned,
            ParsedOrderStatus(
                role=role,
                requested_cloid=owned.cloid,
                state=VenueOrderState.MISSING,
                venue_status=None,
                status_timestamp_ms=None,
                oid=None,
                symbol=None,
                remaining_size=None,
                original_size=None,
                is_trigger=None,
                reduce_only=None,
            ),
        )
    if set(root) != {"status", "order"} or root["status"] != "order":
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus root is unsupported"
        )
    outer = _mapping(root["order"], f"orderStatus[{role}].order")
    if set(outer) != {"order", "status", "statusTimestamp"}:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus record fields are unsupported"
        )
    venue_status = outer["status"]
    if not isinstance(venue_status, str) or venue_status not in _ORDER_STATUSES:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus venue status is unknown"
        )
    status_time = _integer(
        outer["statusTimestamp"], f"orderStatus[{role}].statusTimestamp"
    )
    if status_time > now_ms:
        raise HyperliquidReconcileResponseError(
            "auxiliary order status timestamp is in the future"
        )
    order = _mapping(outer["order"], f"orderStatus[{role}].order.order")
    if set(order) != _ORDER_FIELDS:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus order fields are unsupported"
        )
    if order["cloid"] != owned.cloid or order["coin"] != owned.symbol:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus returned a foreign order"
        )
    if order["side"] != owned.side.wire_value:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus side differs from durable recovery"
        )
    remaining = _exact_decimal(order["sz"], "orderStatus.sz", nonnegative=True)
    original = _exact_decimal(
        order["origSz"], "orderStatus.origSz", nonnegative=True
    )
    if original != owned.requested_quantity or remaining > original:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus size differs from durable recovery"
        )
    is_trigger = _bool(order["isTrigger"], "orderStatus.isTrigger")
    reduce_only = _bool(order["reduceOnly"], "orderStatus.reduceOnly")
    if is_trigger != owned.is_trigger or reduce_only != owned.reduce_only:
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus flags differ from durable recovery"
        )
    _exact_decimal(order["limitPx"], "orderStatus.limitPx", positive=True)
    trigger_price = _exact_decimal(
        order["triggerPx"], "orderStatus.triggerPx", nonnegative=True
    )
    if is_trigger != (trigger_price > _ZERO):
        raise HyperliquidReconcileResponseError(
            "auxiliary orderStatus trigger fields disagree"
        )
    oid = _integer(order["oid"], "orderStatus.oid", maximum=2**63 - 1)
    order_time = _integer(order["timestamp"], "orderStatus.timestamp")
    if order_time > status_time:
        raise HyperliquidReconcileResponseError(
            "auxiliary order timestamp postdates status"
        )
    _bool(order["isPositionTpsl"], "orderStatus.isPositionTpsl")
    _text(order["triggerCondition"], "orderStatus.triggerCondition")
    _text(order["orderType"], "orderStatus.orderType")
    if order["tif"] is not None:
        _text(order["tif"], "orderStatus.tif", maximum=64)
    _array(order["children"], "orderStatus.children", maximum=20)
    return AuxiliaryOrderEvidence(
        owned,
        ParsedOrderStatus(
            role=role,
            requested_cloid=owned.cloid,
            state=VenueOrderState.ORDER,
            venue_status=venue_status,
            status_timestamp_ms=status_time,
            oid=oid,
            symbol=owned.symbol,
            remaining_size=remaining,
            original_size=original,
            is_trigger=is_trigger,
            reduce_only=reduce_only,
        ),
    )


def _durable_recovery_close_orders(
    store: ExecutionStore,
    *,
    parent_command_id: str,
    symbol: str,
) -> tuple[AuxiliaryOwnedOrder, ...]:
    """Load only recovery closes that reached the durable send boundary."""

    if not isinstance(store, ExecutionStore):
        raise TypeError("store must be ExecutionStore")
    if store.environment.value != "testnet":
        raise ValidationError("auxiliary recovery attribution is testnet-only")
    orders: list[AuxiliaryOwnedOrder] = []
    for command in store.list_recovery_commands():
        if (
            command.parent_command_id != parent_command_id
            or command.kind != "reduce_only_close"
            or command.state == "terminal"
        ):
            continue
        try:
            attempt = store.get_recovery_attempt(command.recovery_command_id)
        except RecordNotFound:
            continue
        if attempt.state == "prepared":
            # No submission authority was consumed, so this signed artifact
            # was unreachable by the transport and cannot own a venue fill.
            continue
        if attempt.state not in {"sending", "response_received", "unknown"}:
            raise StateConflict("recovery attempt state is unsupported")
        signed = store.get_signed_recovery_evidence(command.recovery_command_id)
        if (
            signed.evidence_hash != attempt.signed_evidence_hash
            or signed.recovery_hash != command.recovery_hash
        ):
            raise StateConflict("recovery signed evidence differs from its command")
        if attempt.state in {"response_received", "unknown"}:
            transport = store.get_recovery_transport_evidence(
                command.recovery_command_id
            )
            if (
                transport.attempt_id != attempt.attempt_id
                or transport.signed_evidence_hash != attempt.signed_evidence_hash
            ):
                raise StateConflict(
                    "recovery transport does not prove one attempted venue write"
                )
            transport_proves_boundary = (
                transport.evidence_basis == "transport_result"
                and transport.venue_write_attempted is True
            ) or (
                transport.evidence_basis == "claim_expiry"
                and attempt.state == "unknown"
                and transport.outcome == "unknown"
                and transport.venue_write_attempted is None
            )
            if not transport_proves_boundary:
                raise StateConflict(
                    "recovery transport did not cross the durable send boundary"
                )
        try:
            material = json.loads(command.recovery_material_json)
        except (TypeError, ValueError) as error:
            raise StateConflict("recovery material is not valid JSON") from error
        if (
            not isinstance(material, dict)
            or canonical_json(material) != command.recovery_material_json
            or hashlib.sha256(command.recovery_material_json.encode("utf-8")).hexdigest()
            != command.recovery_material_hash
        ):
            raise StateConflict("recovery material encoding differs from durable state")
        try:
            action = recovery_action_from_material(material)
        except (TypeError, ValidationError) as error:
            raise StateConflict("recovery material violates its schema") from error
        if (
            not isinstance(action, ReduceOnlyCloseAction)
            or action.recovery_hash != command.recovery_hash
            or action.symbol != symbol
            or action.account_id != store.account_id
        ):
            raise StateConflict("recovery close differs from parent lifecycle")
        orders.append(
            AuxiliaryOwnedOrder(
                owner_kind="recovery_close",
                owner_id=command.recovery_command_id,
                source_hash=command.recovery_hash,
                role="recovery_close",
                cloid=action.cloid,
                symbol=action.symbol,
                side=(
                    OrderSide.SELL
                    if action.original_signed_position > _ZERO
                    else OrderSide.BUY
                ),
                requested_quantity=action.close_size,
                is_trigger=False,
                reduce_only=True,
                expires_after_ms=signed.expires_after_ms,
            )
        )
    if len(orders) > 4:
        raise StateConflict("multiple unresolved recovery close attempts are unsafe")
    ordered = tuple(sorted(orders, key=lambda item: item.owner_id))
    if len({item.cloid for item in ordered}) != len(ordered):
        raise StateConflict("recovery close CLOID is reused across durable attempts")
    return ordered


_FILL_REQUIRED_FIELDS = {
    "closedPnl",
    "coin",
    "crossed",
    "dir",
    "hash",
    "oid",
    "px",
    "side",
    "startPosition",
    "sz",
    "time",
    "fee",
    "feeToken",
    "tid",
}
_FILL_OPTIONAL_FIELDS = {"builderFee", "liquidation"}


@dataclass(frozen=True, slots=True)
class _RawFill:
    identity: tuple[int, str, int, int]
    oid: int
    tid: int
    transaction_hash: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    signed_quantity: Decimal
    start_position: Decimal
    end_position: Decimal
    price: Decimal
    fee: Decimal
    closed_pnl: Decimal
    fee_token: str
    crossed: bool
    builder_fee: Decimal | None
    time_ms: int

    def canonical_record(self) -> dict[str, object]:
        return {
            "oid": self.oid,
            "tid": self.tid,
            "transaction_hash": self.transaction_hash,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": canonical_decimal(self.quantity),
            "signed_quantity": canonical_decimal(self.signed_quantity),
            "start_position": canonical_decimal(self.start_position),
            "end_position": canonical_decimal(self.end_position),
            "price": canonical_decimal(self.price),
            "fee": canonical_decimal(self.fee),
            "closed_pnl": canonical_decimal(self.closed_pnl),
            "fee_token": self.fee_token,
            "crossed": self.crossed,
            "builder_fee": (
                None
                if self.builder_fee is None
                else canonical_decimal(self.builder_fee)
            ),
            "time_ms": self.time_ms,
        }


def _parse_fill(
    value: object,
    index: int,
    *,
    start_time_ms: int,
    end_time_ms: int,
    now_ms: int,
) -> _RawFill:
    field = f"userFillsByTime[{index}]"
    root = _mapping(value, field)
    if not _FILL_REQUIRED_FIELDS.issubset(root) or not set(root).issubset(
        _FILL_REQUIRED_FIELDS | _FILL_OPTIONAL_FIELDS
    ):
        raise HyperliquidReconcileResponseError(f"{field} fields are unsupported")
    if root.get("liquidation") is not None:
        raise HyperliquidReconcileResponseError("liquidation fills are unsupported in v1")
    symbol = root["coin"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise HyperliquidReconcileResponseError(f"{field}.coin is invalid")
    side_wire = root["side"]
    if side_wire == "B":
        side = OrderSide.BUY
    elif side_wire == "A":
        side = OrderSide.SELL
    else:
        raise HyperliquidReconcileResponseError(f"{field}.side is unsupported")
    quantity = _exact_decimal(root["sz"], f"{field}.sz", positive=True)
    signed = quantity if side is OrderSide.BUY else -quantity
    start_position = _exact_decimal(root["startPosition"], f"{field}.startPosition")
    with localcontext(_EXACT_CONTEXT) as context:
        end_position = context.add(start_position, signed)
    validate_decimal_bounds(end_position, field=f"{field}.endPosition")
    price = _exact_decimal(root["px"], f"{field}.px", positive=True)
    fee = _exact_decimal(root["fee"], f"{field}.fee", nonnegative=True)
    builder_fee = None
    if "builderFee" in root:
        builder_fee = _exact_decimal(
            root["builderFee"], f"{field}.builderFee", nonnegative=True
        )
        if builder_fee > fee:
            raise HyperliquidReconcileResponseError("builder fee exceeds total fee")
    closed_pnl = _exact_decimal(root["closedPnl"], f"{field}.closedPnl")
    crossed = _bool(root["crossed"], f"{field}.crossed")
    # ``dir`` is presentation text only.  Validate bounded text but never use
    # it for side, position, role, or fill arithmetic.
    _text(root["dir"], f"{field}.dir", maximum=128)
    tx_hash = root["hash"]
    if not isinstance(tx_hash, str) or not _TX_HASH_RE.fullmatch(tx_hash):
        raise HyperliquidReconcileResponseError(f"{field}.hash is invalid")
    oid = _integer(root["oid"], f"{field}.oid", maximum=2**63 - 1)
    tid = _integer(root["tid"], f"{field}.tid", maximum=2**63 - 1)
    time_ms = _integer(root["time"], f"{field}.time")
    if not start_time_ms <= time_ms <= end_time_ms:
        raise HyperliquidReconcileResponseError("fill lies outside requested range")
    if time_ms > now_ms:
        raise HyperliquidReconcileResponseError("fill timestamp is in the future")
    fee_token = _text(root["feeToken"], f"{field}.feeToken", maximum=64)
    if not _TOKEN_RE.fullmatch(fee_token):
        raise HyperliquidReconcileResponseError(f"{field}.feeToken is invalid")
    return _RawFill(
        identity=(time_ms, tx_hash, tid, oid),
        oid=oid,
        tid=tid,
        transaction_hash=tx_hash,
        symbol=symbol,
        side=side,
        quantity=quantity,
        signed_quantity=signed,
        start_position=start_position,
        end_position=end_position,
        price=price,
        fee=fee,
        closed_pnl=closed_pnl,
        fee_token=fee_token,
        crossed=crossed,
        builder_fee=builder_fee,
        time_ms=time_ms,
    )


def _fetch_fills(
    endpoint: str,
    account: str,
    *,
    start_time_ms: int,
    end_time_ms: int,
    now_ms: int,
    transport: InfoTransport,
) -> tuple[tuple[_RawFill, ...], FillCoverage]:
    cursor = start_time_ms
    by_identity: dict[tuple[int, str, int, int], _RawFill] = {}
    page_count = 0
    returned_rows = 0
    duplicates = 0
    page_saturated = False
    retention_limited = False
    complete = False
    reason = "maximum_fill_pages_exhausted"
    required_overlap: set[tuple[int, str, int, int]] = set()
    while page_count < _MAX_FILL_PAGES:
        response = _post_info(
            endpoint,
            {
                "type": "userFillsByTime",
                "user": account,
                "startTime": cursor,
                "endTime": end_time_ms,
                "aggregateByTime": False,
            },
            transport,
        )
        rows = _array(
            response,
            f"userFillsByTime page {page_count + 1}",
            maximum=USER_FILLS_PAGE_LIMIT,
        )
        page_count += 1
        returned_rows += len(rows)
        page: list[_RawFill] = []
        for index, value in enumerate(rows):
            fill = _parse_fill(
                value,
                index,
                start_time_ms=cursor,
                end_time_ms=end_time_ms,
                now_ms=now_ms,
            )
            page.append(fill)
            previous = by_identity.get(fill.identity)
            if previous is None:
                by_identity[fill.identity] = fill
            elif previous == fill:
                duplicates += 1
            else:
                raise HyperliquidReconcileResponseError(
                    "duplicate fill identity has conflicting economics"
                )
        if any(
            page[index].time_ms > page[index + 1].time_ms
            for index in range(len(page) - 1)
        ):
            raise HyperliquidReconcileResponseError(
                "userFillsByTime page is not ordered by ascending time"
            )
        page_identities = {item.identity for item in page}
        if required_overlap and not required_overlap.issubset(page_identities):
            raise HyperliquidReconcileResponseError(
                "userFillsByTime inclusive page overlap is incomplete"
            )
        if len(by_identity) >= USER_FILLS_RETENTION_LIMIT:
            retention_limited = True
            reason = "latest_10000_fill_retention_limit"
            break
        if len(rows) < USER_FILLS_PAGE_LIMIT:
            complete = True
            reason = "range_exhausted"
            break
        if not page:
            reason = "full_page_without_parseable_fill"
            break
        next_cursor = page[-1].time_ms
        if next_cursor <= cursor:
            page_saturated = True
            reason = "inclusive_page_boundary_saturated"
            break
        required_overlap = {
            item.identity for item in page if item.time_ms == next_cursor
        }
        cursor = next_cursor
    else:
        page_saturated = True
        reason = "maximum_fill_pages_exhausted"
    complete = complete and not retention_limited and not page_saturated
    ordered = tuple(
        by_identity[key]
        for key in sorted(by_identity)
    )
    coverage = FillCoverage(
        requested_start_time_ms=start_time_ms,
        requested_end_time_ms=end_time_ms,
        page_count=page_count,
        page_limit=USER_FILLS_PAGE_LIMIT,
        retention_limit=USER_FILLS_RETENTION_LIMIT,
        returned_rows=returned_rows,
        unique_fills=len(ordered),
        duplicate_fills=duplicates,
        unmatched_fills=0,
        page_saturated=page_saturated,
        retention_limited=retention_limited,
        complete=complete,
        reason=reason,
    )
    return ordered, coverage


def _leg_status(
    status: ParsedOrderStatus,
    leg: OwnedLeg,
    cumulative: Decimal,
) -> str:
    if cumulative > leg.requested_quantity:
        raise HyperliquidReconcileResponseError("fills exceed owned requested quantity")
    if status.state is VenueOrderState.MISSING:
        return "absent"
    venue = status.venue_status
    if venue == "open":
        if cumulative == leg.requested_quantity:
            raise HyperliquidReconcileResponseError("open order is already fully filled")
        return "partially_filled" if cumulative > _ZERO else "resting"
    if venue == "filled":
        return "filled"
    if venue in _CANCELED_STATUSES:
        return "canceled"
    if venue in _REJECTED_STATUSES:
        if cumulative != _ZERO:
            raise HyperliquidReconcileResponseError("rejected order has fills")
        return "rejected"
    if venue == "triggered":
        return "triggered"
    raise HyperliquidReconcileResponseError("venue order status mapping is incomplete")


def reconcile_hyperliquid_venue(
    snapshot: HyperliquidAccountSnapshot,
    owned_legs: Iterable[OwnedLeg],
    *,
    account_id: str,
    command_id: str,
    plan_hash: str,
    network: HyperliquidNetwork,
    fills_start_time_ms: int,
    transport: InfoTransport = post_public_info,
    clock: Clock = lambda: datetime.now(timezone.utc),
    fills_end_time_ms: int | None = None,
    store: ExecutionStore | None = None,
) -> VenueReconciliationBundle:
    """Build exact, store-ready reconciliation evidence without venue writes."""

    if not isinstance(snapshot, HyperliquidAccountSnapshot):
        raise TypeError("snapshot must be HyperliquidAccountSnapshot")
    if not isinstance(network, HyperliquidNetwork):
        try:
            network = HyperliquidNetwork(network)
        except (TypeError, ValueError) as error:
            raise ValidationError("network must be explicit mainnet or testnet") from error
    if snapshot.network != network.value:
        raise ValidationError("account snapshot network does not match reconciliation")
    selected_legs = tuple(owned_legs)
    if (
        len(selected_legs) != 3
        or any(not isinstance(item, OwnedLeg) for item in selected_legs)
        or {item.role for item in selected_legs} != set(_ROLES)
        or len({item.cloid for item in selected_legs}) != 3
    ):
        raise ValidationError("owned_legs must contain three unique protected roles")
    selected_legs = tuple(sorted(selected_legs, key=lambda item: _ROLES.index(item.role)))
    symbols = {item.symbol for item in selected_legs}
    if len(symbols) != 1:
        raise ValidationError("all owned legs must target one symbol")
    symbol = selected_legs[0].symbol
    checked_account = _input_text(account_id, "account_id")
    checked_command = _input_text(command_id, "command_id")
    if not isinstance(plan_hash, str) or not _HASH_RE.fullmatch(plan_hash):
        raise ValidationError("plan_hash must be a lowercase SHA-256 digest")
    start_ms = _request_integer(fills_start_time_ms, "fills_start_time_ms")
    end_ms = (
        snapshot.server_time_ms
        if fills_end_time_ms is None
        else _request_integer(fills_end_time_ms, "fills_end_time_ms")
    )
    if not start_ms <= end_ms <= snapshot.server_time_ms:
        raise ValidationError("fill range must end within the account snapshot")
    if not callable(transport) or not callable(clock):
        raise TypeError("transport and clock must be callable")
    now_ms = _clock_ms(clock)
    snapshot_age = now_ms - snapshot.server_time_ms
    if snapshot_age > _MAX_SNAPSHOT_AGE_MS or snapshot_age < -_MAX_FUTURE_SKEW_MS:
        raise ValidationError("account snapshot is stale for venue reconciliation")
    endpoint = public_info_endpoint(network.value)
    auxiliary_orders = (
        ()
        if store is None
        else _durable_recovery_close_orders(
            store,
            parent_command_id=checked_command,
            symbol=symbol,
        )
    )
    if store is not None and (
        store.account_id != checked_account or store.environment.value != network.value
    ):
        raise StateConflict("execution store scope differs from reconciliation")
    primary_cloids = {item.cloid for item in selected_legs}
    if primary_cloids & {item.cloid for item in auxiliary_orders}:
        raise StateConflict("parent and recovery orders reuse a CLOID")
    statuses = tuple(
        _parse_order_status(
            _post_info(
                endpoint,
                {
                    "type": "orderStatus",
                    "user": snapshot.main_account_address,
                    "oid": leg.cloid,
                },
                transport,
            ),
            leg,
            now_ms=now_ms,
        )
        for leg in selected_legs
    )
    auxiliary_statuses = tuple(
        _parse_auxiliary_order_status(
            _post_info(
                endpoint,
                {
                    "type": "orderStatus",
                    "user": snapshot.main_account_address,
                    "oid": order.cloid,
                },
                transport,
            ),
            order,
            now_ms=now_ms,
        )
        for order in auxiliary_orders
    )
    known_oids = [item.oid for item in statuses if item.oid is not None]
    auxiliary_oids = [
        item.status.oid
        for item in auxiliary_statuses
        if item.status.oid is not None
    ]
    if len(set(known_oids + auxiliary_oids)) != len(known_oids) + len(
        auxiliary_oids
    ):
        raise HyperliquidReconcileResponseError("orderStatus repeats a venue OID")
    raw_fills, fill_coverage = _fetch_fills(
        endpoint,
        snapshot.main_account_address,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        now_ms=now_ms,
        transport=transport,
    )
    status_by_oid = {
        status.oid: (status, leg)
        for status, leg in zip(statuses, selected_legs)
        if status.oid is not None
    }
    auxiliary_by_oid = {
        evidence.status.oid: evidence
        for evidence in auxiliary_statuses
        if evidence.status.oid is not None
    }
    persisted_recovery_fills: dict[
        tuple[int, str, int, int], tuple[RecoveryVenueFill, str]
    ] = {}
    if store is not None:
        for persisted in store.list_recovery_fills(
            parent_command_id=checked_command
        ):
            owner = store.get_recovery_command(persisted.recovery_command_id)
            if owner.state != "terminal" or owner.kind != "reduce_only_close":
                continue
            occurred_ms = int(persisted.occurred_at.timestamp() * 1_000)
            if not start_ms <= occurred_ms <= end_ms:
                continue
            identity = (
                occurred_ms,
                persisted.transaction_hash,
                persisted.venue_trade_id,
                persisted.venue_oid,
            )
            if identity in persisted_recovery_fills:
                raise StateConflict("persisted recovery fill identity is repeated")
            persisted_recovery_fills[identity] = (persisted, owner.recovery_hash)
    signed_fills: list[SignedFillEvidence] = []
    auxiliary_fills: list[AuxiliaryFillEvidence] = []
    observed_persisted_identities: set[tuple[int, str, int, int]] = set()
    venue_fills: list[VenueFill] = []
    unmatched = 0
    for fill in raw_fills:
        match = status_by_oid.get(fill.oid)
        if match is None:
            auxiliary = auxiliary_by_oid.get(fill.oid)
            if auxiliary is None:
                persisted_match = persisted_recovery_fills.get(fill.identity)
                if persisted_match is None:
                    unmatched += 1
                    continue
                persisted, recovery_hash = persisted_match
                observed_persisted_identities.add(fill.identity)
                if (
                    persisted.fill_id
                    != f"hyperliquid:{fill.symbol}:{fill.time_ms}:{fill.tid}:{fill.oid}"
                    or persisted.symbol != fill.symbol
                    or persisted.side != fill.side.value
                    or persisted.quantity != fill.quantity
                    or persisted.signed_quantity != fill.signed_quantity
                    or persisted.start_position != fill.start_position
                    or persisted.end_position != fill.end_position
                    or persisted.price != fill.price
                    or persisted.fee != fill.fee
                    or persisted.closed_pnl != fill.closed_pnl
                    or persisted.fee_token != fill.fee_token
                    or persisted.crossed is not fill.crossed
                    or persisted.builder_fee != fill.builder_fee
                ):
                    raise HyperliquidReconcileResponseError(
                        "venue recovery fill differs from durable economics"
                    )
                auxiliary_fills.append(
                    AuxiliaryFillEvidence(
                        owner_kind="recovery_close",
                        owner_id=persisted.recovery_command_id,
                        source_hash=recovery_hash,
                        fill=SignedFillEvidence(
                            fill_id=persisted.fill_id,
                            role="recovery_close",
                            cloid=persisted.cloid,
                            oid=fill.oid,
                            tid=fill.tid,
                            transaction_hash=fill.transaction_hash,
                            symbol=fill.symbol,
                            side=fill.side,
                            quantity=fill.quantity,
                            signed_quantity=fill.signed_quantity,
                            start_position=fill.start_position,
                            end_position=fill.end_position,
                            price=fill.price,
                            fee=fill.fee,
                            closed_pnl=fill.closed_pnl,
                            fee_token=fill.fee_token,
                            crossed=fill.crossed,
                            builder_fee=fill.builder_fee,
                            time_ms=fill.time_ms,
                        ),
                    )
                )
                continue
            owned = auxiliary.order
            if fill.symbol != owned.symbol or fill.side is not owned.side:
                raise HyperliquidReconcileResponseError(
                    "fill differs from its recovery-owned order"
                )
            auxiliary_fills.append(
                AuxiliaryFillEvidence(
                    owner_kind=owned.owner_kind,
                    owner_id=owned.owner_id,
                    source_hash=owned.source_hash,
                    fill=SignedFillEvidence(
                        fill_id=(
                            f"hyperliquid:{fill.symbol}:{fill.time_ms}:"
                            f"{fill.tid}:{fill.oid}"
                        ),
                        role=owned.role,
                        cloid=owned.cloid,
                        oid=fill.oid,
                        tid=fill.tid,
                        transaction_hash=fill.transaction_hash,
                        symbol=fill.symbol,
                        side=fill.side,
                        quantity=fill.quantity,
                        signed_quantity=fill.signed_quantity,
                        start_position=fill.start_position,
                        end_position=fill.end_position,
                        price=fill.price,
                        fee=fill.fee,
                        closed_pnl=fill.closed_pnl,
                        fee_token=fill.fee_token,
                        crossed=fill.crossed,
                        builder_fee=fill.builder_fee,
                        time_ms=fill.time_ms,
                    ),
                )
            )
            continue
        _, leg = match
        if fill.symbol != leg.symbol or fill.side is not leg.side:
            raise HyperliquidReconcileResponseError("fill differs from its owned order")
        fill_id = f"hyperliquid:{fill.symbol}:{fill.time_ms}:{fill.tid}:{fill.oid}"
        evidence = SignedFillEvidence(
            fill_id=fill_id,
            role=leg.role,
            cloid=leg.cloid,
            oid=fill.oid,
            tid=fill.tid,
            transaction_hash=fill.transaction_hash,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            signed_quantity=fill.signed_quantity,
            start_position=fill.start_position,
            end_position=fill.end_position,
            price=fill.price,
            fee=fill.fee,
            closed_pnl=fill.closed_pnl,
            fee_token=fill.fee_token,
            crossed=fill.crossed,
            builder_fee=fill.builder_fee,
            time_ms=fill.time_ms,
        )
        signed_fills.append(evidence)
        venue_fills.append(
            VenueFill(
                fill_id=fill_id,
                role=leg.role,
                cloid=leg.cloid,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                occurred_at=_datetime_ms(fill.time_ms),
                venue_oid=fill.oid,
                venue_trade_id=fill.tid,
                transaction_hash=fill.transaction_hash,
                closed_pnl=fill.closed_pnl,
                fee_token=fill.fee_token,
                observed_at=_datetime_ms(snapshot.server_time_ms),
            )
        )
    auxiliary_quantity: dict[str, Decimal] = {
        item.owner_id: _ZERO for item in auxiliary_orders
    }
    with localcontext(_EXACT_CONTEXT) as context:
        for item in auxiliary_fills:
            if item.owner_id in auxiliary_quantity:
                auxiliary_quantity[item.owner_id] = context.add(
                    auxiliary_quantity[item.owner_id], item.fill.quantity
                )
    for order in auxiliary_orders:
        if auxiliary_quantity[order.owner_id] > order.requested_quantity:
            raise HyperliquidReconcileResponseError(
                "recovery fills exceed the durable close quantity"
            )
    auxiliary_fill_detail_missing = any(
        evidence.status.state is VenueOrderState.ORDER
        and evidence.status.venue_status == "filled"
        and auxiliary_quantity[evidence.order.owner_id]
        != evidence.order.requested_quantity
        for evidence in auxiliary_statuses
    )
    auxiliary_missing_unsettled = any(
        evidence.status.state is VenueOrderState.MISSING
        and evidence.order.expires_after_ms is not None
        and end_ms
        < evidence.order.expires_after_ms + _LATE_WRITE_SETTLEMENT_MS
        for evidence in auxiliary_statuses
    )
    auxiliary_status_nonterminal = any(
        evidence.status.state is VenueOrderState.ORDER
        and evidence.status.venue_status not in _AUXILIARY_TERMINAL_STATUSES
        for evidence in auxiliary_statuses
    )
    persisted_fill_missing = (
        set(persisted_recovery_fills) != observed_persisted_identities
    )
    fill_coverage = FillCoverage(
        requested_start_time_ms=fill_coverage.requested_start_time_ms,
        requested_end_time_ms=fill_coverage.requested_end_time_ms,
        page_count=fill_coverage.page_count,
        page_limit=fill_coverage.page_limit,
        retention_limit=fill_coverage.retention_limit,
        returned_rows=fill_coverage.returned_rows,
        unique_fills=fill_coverage.unique_fills,
        duplicate_fills=fill_coverage.duplicate_fills,
        unmatched_fills=unmatched,
        page_saturated=fill_coverage.page_saturated,
        retention_limited=fill_coverage.retention_limited,
        complete=(
            fill_coverage.complete
            and unmatched == 0
            and not persisted_fill_missing
        ),
        reason=(
            fill_coverage.reason
            if not fill_coverage.complete
            else "unmatched_account_fills"
            if unmatched
            else "persisted_recovery_fill_absent_from_window"
            if persisted_fill_missing
            else "range_exhausted"
        ),
    )
    fills_by_role: dict[str, Decimal] = {role: _ZERO for role in _ROLES}
    with localcontext(_EXACT_CONTEXT) as context:
        for fill in signed_fills:
            fills_by_role[fill.role] = context.add(
                fills_by_role[fill.role],
                fill.quantity,
            )
    legs: list[LegReconciliation] = []
    incomplete: list[str] = []
    if auxiliary_fill_detail_missing:
        incomplete.append("recovery_close_fill_details_incomplete")
    if auxiliary_missing_unsettled:
        incomplete.append("recovery_close_missing_before_signed_expiry_settled")
    if auxiliary_status_nonterminal:
        incomplete.append("recovery_close_order_not_terminal")
    for status, leg in zip(statuses, selected_legs):
        cumulative = fills_by_role[leg.role]
        mapped_status = _leg_status(status, leg, cumulative)
        if status.venue_status == "filled" and cumulative != leg.requested_quantity:
            incomplete.append(f"{leg.role}_fill_details_incomplete")
            cumulative = leg.requested_quantity
        if status.state is VenueOrderState.MISSING:
            incomplete.append(f"{leg.role}_order_missing")
        if (
            status.status_timestamp_ms is not None
            and status.status_timestamp_ms > snapshot.server_time_ms
        ):
            incomplete.append(f"{leg.role}_status_postdates_account_snapshot")
        legs.append(
            LegReconciliation(
                role=leg.role,
                cloid=leg.cloid,
                status=mapped_status,
                cumulative_filled=cumulative,
                venue_oid=status.oid,
            )
        )
    if not fill_coverage.complete:
        incomplete.append(fill_coverage.reason)

    position = snapshot.position(symbol)
    signed_position = _ZERO if position is None else position.signed_size
    ordered_signed_fills = sorted(
        [*signed_fills, *(item.fill for item in auxiliary_fills)],
        key=lambda item: (item.time_ms, item.tid, item.oid),
    )
    for left, right in zip(ordered_signed_fills, ordered_signed_fills[1:]):
        if left.end_position != right.start_position:
            incomplete.append("fill_position_chain_discontinuous")
            break
    if ordered_signed_fills and ordered_signed_fills[0].start_position != _ZERO:
        incomplete.append("fill_start_position_differs_from_flat_preflight")
    if (
        ordered_signed_fills
        and unmatched == 0
        and ordered_signed_fills[-1].end_position != signed_position
    ):
        incomplete.append("fill_end_position_differs_from_account")
    if signed_position != _ZERO and not ordered_signed_fills:
        incomplete.append("position_without_owned_fill_chain")
    stop_status = statuses[_ROLES.index("protective_stop")]
    stop_cloid = selected_legs[_ROLES.index("protective_stop")].cloid
    coverage = snapshot.protection_coverage(
        symbol,
        expected_stop_cloids=(stop_cloid,),
    )
    protected = (
        coverage.covered_size
        if stop_status.state is VenueOrderState.ORDER
        and stop_status.venue_status == "open"
        else _ZERO
    )
    account_reconciliation = snapshot.reconcile(
        owned_cloids=tuple(item.cloid for item in selected_legs),
        allowed_position_symbols=(symbol,),
        expected_stop_cloids_by_symbol={symbol: (stop_cloid,)},
    )
    if account_reconciliation.foreign_order_oids:
        incomplete.append("foreign_open_orders")
    if account_reconciliation.foreign_position_symbols:
        incomplete.append("foreign_positions")
    if account_reconciliation.orphan_protection_oids:
        incomplete.append("orphan_protection")
    if any(not item.fully_protected for item in account_reconciliation.protection):
        incomplete.append("position_not_fully_protected")

    reasons = tuple(dict.fromkeys(incomplete))
    complete = not reasons
    observed_at = _datetime_ms(snapshot.server_time_ms)
    material = {
        "network": network.value,
        "main_account_address": snapshot.main_account_address,
        "account_id": checked_account,
        "command_id": checked_command,
        "plan_hash": plan_hash,
        "account_snapshot_hash": snapshot.snapshot_hash,
        "observed_at": observed_at,
        "order_statuses": [item.canonical_record() for item in statuses],
        "signed_fills": [item.canonical_record() for item in signed_fills],
        "auxiliary_order_statuses": [
            item.canonical_record() for item in auxiliary_statuses
        ],
        "auxiliary_fills": [
            item.canonical_record() for item in auxiliary_fills
        ],
        "fill_coverage": fill_coverage.as_dict(),
        "legs": [item.as_dict() for item in legs],
        "signed_position_quantity": canonical_decimal(signed_position),
        "protected_quantity": canonical_decimal(protected),
        "complete": complete,
        "incomplete_reasons": list(reasons),
    }
    return VenueReconciliationBundle(
        network=network,
        main_account_address=snapshot.main_account_address,
        account_id=checked_account,
        command_id=checked_command,
        plan_hash=plan_hash,
        account_snapshot_hash=snapshot.snapshot_hash,
        observed_at=observed_at,
        order_statuses=statuses,
        signed_fills=tuple(signed_fills),
        fill_coverage=fill_coverage,
        legs=tuple(legs),
        fills=tuple(venue_fills),
        signed_position_quantity=signed_position,
        protected_quantity=protected,
        complete=complete,
        incomplete_reasons=reasons,
        reconciliation_hash=domain_hash(VENUE_RECONCILIATION_HASH_DOMAIN, material),
        auxiliary_order_statuses=auxiliary_statuses,
        auxiliary_fills=tuple(auxiliary_fills),
    )


__all__ = (
    "FILL_LOOKBACK_MS",
    "USER_FILLS_PAGE_LIMIT",
    "USER_FILLS_RETENTION_LIMIT",
    "VENUE_RECONCILIATION_HASH_DOMAIN",
    "FillCoverage",
    "AuxiliaryFillEvidence",
    "AuxiliaryOrderEvidence",
    "AuxiliaryOwnedOrder",
    "HyperliquidReconcileError",
    "HyperliquidReconcileResponseError",
    "HyperliquidReconcileTransportError",
    "OwnedLeg",
    "ParsedOrderStatus",
    "SignedFillEvidence",
    "VenueOrderState",
    "VenueReconciliationBundle",
    "canonical_hyperliquid_fill_id",
    "reconcile_hyperliquid_venue",
)
