"""Strict read-only Hyperliquid account and reconciliation snapshots.

This module can only call allowlisted public ``/info`` requests.  It has no
credential, signing, nonce, or ``/exchange`` capability.  A snapshot is bound
to one explicitly supplied *main account* address; that address is reused for
every user-specific request so an API-wallet address cannot be substituted by
an adapter between reads.

Version one supports Hyperliquid standard account semantics only.  The API's
``default`` and ``disabled`` abstraction responses are accepted; unified,
portfolio-margin, DEX-abstraction, and unknown modes fail closed because their
balance source and margin model differ.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from enum import Enum
import re
from typing import Any, TypeAlias

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .errors import HarnessError, ValidationError
from .market_data import post_public_info, public_info_endpoint


InfoTransport: TypeAlias = Callable[[str, Mapping[str, object]], object]
Clock: TypeAlias = Callable[[], datetime]

ACCOUNT_SNAPSHOT_HASH_DOMAIN = "trading-harness/hyperliquid-account-snapshot/v1"
METADATA_SNAPSHOT_HASH_DOMAIN = "trading-harness/hyperliquid-perp-metadata/v1"
ACCOUNT_SNAPSHOT_SCHEMA_VERSION = "hyperliquid.account_snapshot.v1"

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TIMESTAMP_MS = 253_402_300_799_999
_MAX_OPEN_ORDERS = 5_000
_MAX_ORDER_TREE_DEPTH = 2
_STANDARD_ACCOUNT_MODES = frozenset({"default", "disabled"})
_UNSUPPORTED_ACCOUNT_MODES = frozenset(
    {"unifiedAccount", "portfolioMargin", "dexAbstraction"}
)


class HyperliquidAccountError(HarnessError):
    """Base class for expected account-snapshot failures."""


class HyperliquidAccountTransportError(HyperliquidAccountError):
    """The allowlisted public info endpoint could not be read."""


class HyperliquidAccountResponseError(HyperliquidAccountError, ValueError):
    """A public account response violated its expected schema."""


class UnsupportedAccountModeError(HyperliquidAccountResponseError):
    """The account uses margin semantics not implemented by version one."""


class StaleAccountSnapshotError(HyperliquidAccountResponseError):
    """The clearinghouse state time is stale or implausibly in the future."""


class StandardAccountMode(str, Enum):
    DEFAULT = "default"
    DISABLED = "disabled"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def wire_value(self) -> str:
        return "B" if self is OrderSide.BUY else "A"


class TriggerKind(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    UNKNOWN = "unknown"


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HyperliquidAccountResponseError(f"{field} must be a JSON object")
    return value


def _array(value: object, field: str, *, maximum: int | None = None) -> list[object]:
    if not isinstance(value, list):
        raise HyperliquidAccountResponseError(f"{field} must be a JSON array")
    if maximum is not None and len(value) > maximum:
        raise HyperliquidAccountResponseError(
            f"{field} exceeds the supported limit of {maximum}"
        )
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise HyperliquidAccountResponseError(f"{field} must be boolean")
    return value


def _integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_TIMESTAMP_MS,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HyperliquidAccountResponseError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise HyperliquidAccountResponseError(
            f"{field} must be from {minimum} to {maximum}"
        )
    return value


def _request_integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be from {minimum} to {maximum}")
    return value


def _text(value: object, field: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise HyperliquidAccountResponseError(
            f"{field} must be bounded, printable, non-empty text"
        )
    return value


def _exact_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HyperliquidAccountResponseError(
            f"{field} must be an exact decimal string"
        )
    try:
        result = Decimal(value)
        validate_decimal_bounds(result, field=field)
    except (DecimalException, ValueError) as error:
        raise HyperliquidAccountResponseError(
            f"{field} must be a bounded finite decimal string"
        ) from error
    if positive and result <= _ZERO:
        raise HyperliquidAccountResponseError(f"{field} must be greater than zero")
    if nonnegative and result < _ZERO:
        raise HyperliquidAccountResponseError(f"{field} must be non-negative")
    return result


def _exact_integer_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HyperliquidAccountResponseError(
            f"{field} must be an exact integer or decimal string"
        )
    if isinstance(value, int):
        result = Decimal(value)
    else:
        result = _exact_decimal(value, field)
    if result != result.to_integral_value():
        raise HyperliquidAccountResponseError(f"{field} must be an exact integer")
    if positive and result <= _ZERO:
        raise HyperliquidAccountResponseError(f"{field} must be greater than zero")
    return result


def _optional_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    return _exact_decimal(value, field, positive=positive)


def _clock_read(clock: Clock) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise ValidationError(f"clock failed: {type(error).__name__}") from error
    if not isinstance(value, datetime):
        raise ValidationError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("clock must return a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError("clock returned an unsupported datetime") from error


def _datetime_to_ms(value: datetime) -> int:
    delta = value - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if not 0 <= result <= _MAX_TIMESTAMP_MS:
        raise ValidationError("clock is outside the supported timestamp range")
    return result


def _iso_ms(value: int) -> str:
    return (_EPOCH + timedelta(milliseconds=value)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _address(value: object, field: str = "main_account_address") -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase 20-byte address")
    return value


def _symbol(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
        raise HyperliquidAccountResponseError(f"{field} is not a canonical symbol")
    return value


def _cloid(value: object, field: str) -> str:
    if not isinstance(value, str) or not _CLOID_RE.fullmatch(value):
        raise HyperliquidAccountResponseError(
            f"{field} must be a lowercase 128-bit CLOID"
        )
    return value


def _input_cloid(value: object, field: str) -> str:
    if not isinstance(value, str) or not _CLOID_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase 128-bit CLOID")
    return value


def _input_symbol(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a canonical symbol")
    return value


@dataclass(frozen=True, slots=True)
class PerpInstrument:
    symbol: str
    asset_id: int
    sz_decimals: int
    max_leverage: Decimal
    margin_mode: str
    margin_table_id: int | None
    is_delisted: bool
    metadata_hash: str

    def canonical_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "asset_id": self.asset_id,
            "sz_decimals": self.sz_decimals,
            "max_leverage": canonical_decimal(self.max_leverage),
            "margin_mode": self.margin_mode,
            "margin_table_id": self.margin_table_id,
            "is_delisted": self.is_delisted,
            "metadata_hash": self.metadata_hash,
        }

    def to_wire_metadata(self) -> Any:
        """Convert to the exact metadata accepted by the wire builder."""

        from .hyperliquid_wire import PerpInstrumentMetadata

        return PerpInstrumentMetadata(
            symbol=self.symbol,
            asset_id=self.asset_id,
            sz_decimals=self.sz_decimals,
            max_leverage=self.max_leverage,
            margin_mode=self.margin_mode,
            is_delisted=self.is_delisted,
            source_hash=self.metadata_hash,
        )


@dataclass(frozen=True, slots=True)
class PerpMetadataSnapshot:
    collateral_token: int | None
    instruments: tuple[PerpInstrument, ...]
    metadata_hash: str

    def instrument(self, symbol: str) -> PerpInstrument:
        checked = _input_symbol(symbol, "symbol")
        matches = [item for item in self.instruments if item.symbol == checked]
        if len(matches) != 1:
            raise ValidationError(f"symbol {checked!r} is not uniquely present in metadata")
        return matches[0]

    def canonical_record(self) -> dict[str, object]:
        return {
            "collateral_token": self.collateral_token,
            "instruments": [item.canonical_record() for item in self.instruments],
            "metadata_hash": self.metadata_hash,
        }


@dataclass(frozen=True, slots=True)
class MarginSummary:
    account_value: Decimal
    total_notional_position: Decimal
    total_raw_usd: Decimal
    total_margin_used: Decimal

    def canonical_record(self) -> dict[str, str]:
        return {
            "account_value": canonical_decimal(self.account_value),
            "total_notional_position": canonical_decimal(
                self.total_notional_position
            ),
            "total_raw_usd": canonical_decimal(self.total_raw_usd),
            "total_margin_used": canonical_decimal(self.total_margin_used),
        }


@dataclass(frozen=True, slots=True)
class PerpPosition:
    symbol: str
    asset_id: int
    signed_size: Decimal
    entry_price: Decimal | None
    position_value: Decimal
    unrealized_pnl: Decimal
    margin_used: Decimal
    liquidation_price: Decimal | None
    leverage_type: str
    leverage: Decimal
    leverage_raw_usd: Decimal | None
    max_leverage: Decimal
    return_on_equity: Decimal
    cumulative_funding_all_time: Decimal
    cumulative_funding_since_open: Decimal
    cumulative_funding_since_change: Decimal

    @property
    def side(self) -> PositionSide:
        return PositionSide.LONG if self.signed_size > _ZERO else PositionSide.SHORT

    @property
    def absolute_size(self) -> Decimal:
        return abs(self.signed_size)

    def canonical_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "asset_id": self.asset_id,
            "signed_size": canonical_decimal(self.signed_size),
            "side": self.side.value,
            "entry_price": (
                None if self.entry_price is None else canonical_decimal(self.entry_price)
            ),
            "position_value": canonical_decimal(self.position_value),
            "unrealized_pnl": canonical_decimal(self.unrealized_pnl),
            "margin_used": canonical_decimal(self.margin_used),
            "liquidation_price": (
                None
                if self.liquidation_price is None
                else canonical_decimal(self.liquidation_price)
            ),
            "leverage_type": self.leverage_type,
            "leverage": canonical_decimal(self.leverage),
            "leverage_raw_usd": (
                None
                if self.leverage_raw_usd is None
                else canonical_decimal(self.leverage_raw_usd)
            ),
            "max_leverage": canonical_decimal(self.max_leverage),
            "return_on_equity": canonical_decimal(self.return_on_equity),
            "cumulative_funding": {
                "all_time": canonical_decimal(self.cumulative_funding_all_time),
                "since_open": canonical_decimal(self.cumulative_funding_since_open),
                "since_change": canonical_decimal(
                    self.cumulative_funding_since_change
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class OpenOrder:
    symbol: str
    asset_id: int
    oid: int
    cloid: str | None
    side: OrderSide
    remaining_size: Decimal
    original_size: Decimal
    limit_price: Decimal
    order_type: str
    tif: str | None
    timestamp_ms: int
    is_trigger: bool
    trigger_price: Decimal
    trigger_condition: str
    trigger_kind: TriggerKind | None
    reduce_only: bool
    is_position_tpsl: bool
    children: tuple["OpenOrder", ...]

    @property
    def is_protective_stop(self) -> bool:
        return (
            self.is_trigger
            and self.reduce_only
            and self.trigger_kind is TriggerKind.STOP_LOSS
        )

    def walk(self) -> tuple["OpenOrder", ...]:
        result: list[OpenOrder] = [self]
        for child in self.children:
            result.extend(child.walk())
        return tuple(result)

    def canonical_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "asset_id": self.asset_id,
            "oid": self.oid,
            "cloid": self.cloid,
            "side": self.side.value,
            "remaining_size": canonical_decimal(self.remaining_size),
            "original_size": canonical_decimal(self.original_size),
            "limit_price": canonical_decimal(self.limit_price),
            "order_type": self.order_type,
            "tif": self.tif,
            "timestamp_ms": self.timestamp_ms,
            "is_trigger": self.is_trigger,
            "trigger_price": canonical_decimal(self.trigger_price),
            "trigger_condition": self.trigger_condition,
            "trigger_kind": (
                None if self.trigger_kind is None else self.trigger_kind.value
            ),
            "reduce_only": self.reduce_only,
            "is_position_tpsl": self.is_position_tpsl,
            "children": [child.canonical_record() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class ProtectionCoverage:
    symbol: str
    position_side: PositionSide | None
    required_size: Decimal
    covered_size: Decimal
    deficit_size: Decimal
    qualifying_oids: tuple[int, ...]
    qualifying_cloids: tuple[str, ...]
    fully_protected: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "position_side": (
                None if self.position_side is None else self.position_side.value
            ),
            "required_size": canonical_decimal(self.required_size),
            "covered_size": canonical_decimal(self.covered_size),
            "deficit_size": canonical_decimal(self.deficit_size),
            "qualifying_oids": list(self.qualifying_oids),
            "qualifying_cloids": list(self.qualifying_cloids),
            "fully_protected": self.fully_protected,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AccountReconciliation:
    foreign_order_oids: tuple[int, ...]
    foreign_position_symbols: tuple[str, ...]
    orphan_protection_oids: tuple[int, ...]
    protection: tuple[ProtectionCoverage, ...]
    halt_required: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "foreign_order_oids": list(self.foreign_order_oids),
            "foreign_position_symbols": list(self.foreign_position_symbols),
            "orphan_protection_oids": list(self.orphan_protection_oids),
            "protection": [item.as_dict() for item in self.protection],
            "halt_required": self.halt_required,
        }


@dataclass(frozen=True, slots=True)
class HyperliquidAccountSnapshot:
    network: str
    source_url: str
    main_account_address: str
    account_mode: StandardAccountMode
    server_time_ms: int
    received_at_ms: int
    age_ms: int
    margin_summary: MarginSummary
    cross_margin_summary: MarginSummary
    cross_maintenance_margin_used: Decimal
    withdrawable: Decimal
    positions: tuple[PerpPosition, ...]
    open_orders: tuple[OpenOrder, ...]
    metadata: PerpMetadataSnapshot
    snapshot_hash: str

    def position(self, symbol: str) -> PerpPosition | None:
        checked = _input_symbol(symbol, "symbol")
        matches = [item for item in self.positions if item.symbol == checked]
        if len(matches) > 1:
            raise ValidationError("snapshot contains duplicate position symbols")
        return matches[0] if matches else None

    def all_open_orders(self) -> tuple[OpenOrder, ...]:
        result: list[OpenOrder] = []
        for order in self.open_orders:
            result.extend(order.walk())
        return tuple(result)

    def protection_coverage(
        self,
        symbol: str,
        *,
        expected_stop_cloids: Iterable[str],
    ) -> ProtectionCoverage:
        checked_symbol = _input_symbol(symbol, "symbol")
        expected = frozenset(
            _input_cloid(value, "expected_stop_cloids")
            for value in expected_stop_cloids
        )
        position = self.position(checked_symbol)
        if position is None:
            return ProtectionCoverage(
                symbol=checked_symbol,
                position_side=None,
                required_size=_ZERO,
                covered_size=_ZERO,
                deficit_size=_ZERO,
                qualifying_oids=(),
                qualifying_cloids=(),
                fully_protected=True,
                reason="no_open_position",
            )

        closing_side = (
            OrderSide.SELL
            if position.side is PositionSide.LONG
            else OrderSide.BUY
        )
        qualifying = tuple(
            order
            for order in self.open_orders
            if order.symbol == checked_symbol
            and order.cloid in expected
            and order.side is closing_side
            and order.is_protective_stop
        )
        covered = sum(
            (
                position.absolute_size
                if order.is_position_tpsl
                and order.remaining_size == _ZERO
                and order.original_size == _ZERO
                else order.remaining_size
                for order in qualifying
            ),
            start=_ZERO,
        )
        deficit = max(position.absolute_size - covered, _ZERO)
        protected = deficit == _ZERO
        return ProtectionCoverage(
            symbol=checked_symbol,
            position_side=position.side,
            required_size=position.absolute_size,
            covered_size=covered,
            deficit_size=deficit,
            qualifying_oids=tuple(order.oid for order in qualifying),
            qualifying_cloids=tuple(
                order.cloid for order in qualifying if order.cloid is not None
            ),
            fully_protected=protected,
            reason="venue_stop_covers_position" if protected else "stop_coverage_deficit",
        )

    def reconcile(
        self,
        *,
        owned_cloids: Iterable[str],
        allowed_position_symbols: Iterable[str],
        expected_stop_cloids_by_symbol: Mapping[str, Iterable[str]],
    ) -> AccountReconciliation:
        owned = frozenset(
            _input_cloid(value, "owned_cloids") for value in owned_cloids
        )
        allowed = frozenset(
            _input_symbol(value, "allowed_position_symbols")
            for value in allowed_position_symbols
        )
        stop_map: dict[str, frozenset[str]] = {}
        for raw_symbol, raw_cloids in expected_stop_cloids_by_symbol.items():
            symbol = _input_symbol(raw_symbol, "expected_stop_cloids_by_symbol key")
            cloids = frozenset(
                _input_cloid(value, f"expected stop CLOID for {symbol}")
                for value in raw_cloids
            )
            if not cloids.issubset(owned):
                raise ValidationError("expected stop CLOIDs must be owned CLOIDs")
            stop_map[symbol] = cloids

        foreign_orders = tuple(
            sorted(
                order.oid
                for order in self.all_open_orders()
                if order.cloid is None or order.cloid not in owned
            )
        )
        foreign_positions = tuple(
            sorted(position.symbol for position in self.positions if position.symbol not in allowed)
        )
        coverage = tuple(
            self.protection_coverage(
                position.symbol,
                expected_stop_cloids=stop_map.get(position.symbol, ()),
            )
            for position in self.positions
            if position.symbol in allowed
        )
        positioned = {position.symbol for position in self.positions}
        orphan_protection = tuple(
            sorted(
                order.oid
                for order in self.open_orders
                if order.cloid in owned
                and order.is_trigger
                and order.reduce_only
                and order.symbol not in positioned
            )
        )
        halt = bool(
            foreign_orders
            or foreign_positions
            or orphan_protection
            or any(not item.fully_protected for item in coverage)
        )
        return AccountReconciliation(
            foreign_order_oids=foreign_orders,
            foreign_position_symbols=foreign_positions,
            orphan_protection_oids=orphan_protection,
            protection=coverage,
            halt_required=halt,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACCOUNT_SNAPSHOT_SCHEMA_VERSION,
            "venue": "hyperliquid",
            "network": self.network,
            "source_url": self.source_url,
            "main_account_address": self.main_account_address,
            "account_mode": self.account_mode.value,
            "server_time_ms": self.server_time_ms,
            "server_time": _iso_ms(self.server_time_ms),
            "received_at_ms": self.received_at_ms,
            "received_at": _iso_ms(self.received_at_ms),
            "age_ms": self.age_ms,
            "margin_summary": self.margin_summary.canonical_record(),
            "cross_margin_summary": self.cross_margin_summary.canonical_record(),
            "cross_maintenance_margin_used": canonical_decimal(
                self.cross_maintenance_margin_used
            ),
            "withdrawable": canonical_decimal(self.withdrawable),
            "positions": [item.canonical_record() for item in self.positions],
            "open_orders": [item.canonical_record() for item in self.open_orders],
            "metadata": self.metadata.canonical_record(),
            "snapshot_hash_domain": ACCOUNT_SNAPSHOT_HASH_DOMAIN,
            "snapshot_hash": self.snapshot_hash,
            "read_only": True,
        }


def _parse_account_mode(response: object) -> StandardAccountMode:
    if not isinstance(response, str):
        raise HyperliquidAccountResponseError(
            "userAbstraction response must be a string"
        )
    if response in _STANDARD_ACCOUNT_MODES:
        return StandardAccountMode(response)
    if response in _UNSUPPORTED_ACCOUNT_MODES:
        raise UnsupportedAccountModeError(
            f"account mode {response!r} is unsupported by standard-mode v1"
        )
    raise HyperliquidAccountResponseError("userAbstraction response is unsupported")


def _parse_metadata(response: object) -> PerpMetadataSnapshot:
    root = _mapping(response, "meta response")
    allowed_root = {"universe", "marginTables", "collateralToken"}
    if "universe" not in root or not set(root).issubset(allowed_root):
        raise HyperliquidAccountResponseError("meta response fields are unsupported")
    if "marginTables" in root:
        _array(root["marginTables"], "meta.marginTables")
    collateral = (
        None
        if "collateralToken" not in root
        else _integer(
            root["collateralToken"],
            "meta.collateralToken",
            maximum=1_000_000,
        )
    )
    universe = _array(root["universe"], "meta.universe", maximum=10_000)
    if not universe:
        raise HyperliquidAccountResponseError("meta.universe must not be empty")

    allowed_asset = {
        "name",
        "szDecimals",
        "maxLeverage",
        "marginTableId",
        "onlyIsolated",
        "isDelisted",
        "marginMode",
    }
    required_asset = {"name", "szDecimals", "maxLeverage"}
    records: list[dict[str, object]] = []
    preliminary: list[tuple[str, int, Decimal, str, int | None, bool]] = []
    seen: set[str] = set()
    for asset_id, raw in enumerate(universe):
        item = _mapping(raw, f"meta.universe[{asset_id}]")
        if not required_asset.issubset(item) or not set(item).issubset(allowed_asset):
            raise HyperliquidAccountResponseError(
                f"meta.universe[{asset_id}] fields are unsupported"
            )
        symbol = _symbol(item["name"], f"meta.universe[{asset_id}].name")
        folded = symbol.casefold()
        if folded in seen:
            raise HyperliquidAccountResponseError("meta.universe contains duplicate symbols")
        seen.add(folded)
        sz_decimals = _integer(
            item["szDecimals"],
            f"meta.universe[{asset_id}].szDecimals",
            maximum=8,
        )
        max_leverage = _exact_integer_decimal(
            item["maxLeverage"],
            f"meta.universe[{asset_id}].maxLeverage",
            positive=True,
        )
        margin_table_id = (
            None
            if "marginTableId" not in item
            else _integer(
                item["marginTableId"],
                f"meta.universe[{asset_id}].marginTableId",
                maximum=1_000_000,
            )
        )
        only_isolated = (
            False
            if "onlyIsolated" not in item
            else _bool(
                item["onlyIsolated"],
                f"meta.universe[{asset_id}].onlyIsolated",
            )
        )
        if "marginMode" in item:
            margin_mode = _text(
                item["marginMode"],
                f"meta.universe[{asset_id}].marginMode",
                maximum=32,
            )
            if margin_mode not in {"strictIsolated", "noCross"}:
                raise HyperliquidAccountResponseError("unsupported perp marginMode")
            if not only_isolated:
                raise HyperliquidAccountResponseError(
                    "isolated marginMode conflicts with onlyIsolated"
                )
        else:
            margin_mode = "isolated" if only_isolated else "cross"
        is_delisted = (
            False
            if "isDelisted" not in item
            else _bool(
                item["isDelisted"],
                f"meta.universe[{asset_id}].isDelisted",
            )
        )
        record = {
            "symbol": symbol,
            "asset_id": asset_id,
            "sz_decimals": sz_decimals,
            "max_leverage": canonical_decimal(max_leverage),
            "margin_mode": margin_mode,
            "margin_table_id": margin_table_id,
            "is_delisted": is_delisted,
        }
        records.append(record)
        preliminary.append(
            (
                symbol,
                sz_decimals,
                max_leverage,
                margin_mode,
                margin_table_id,
                is_delisted,
            )
        )

    root_material = {
        "collateral_token": collateral,
        "instruments": records,
    }
    root_hash = domain_hash(METADATA_SNAPSHOT_HASH_DOMAIN, root_material)
    instruments = tuple(
        PerpInstrument(
            symbol=symbol,
            asset_id=asset_id,
            sz_decimals=sz_decimals,
            max_leverage=max_leverage,
            margin_mode=margin_mode,
            margin_table_id=margin_table_id,
            is_delisted=is_delisted,
            metadata_hash=domain_hash(
                "trading-harness/hyperliquid-perp-instrument/v1",
                {**records[asset_id], "metadata_snapshot_hash": root_hash},
            ),
        )
        for asset_id, (
            symbol,
            sz_decimals,
            max_leverage,
            margin_mode,
            margin_table_id,
            is_delisted,
        ) in enumerate(preliminary)
    )
    return PerpMetadataSnapshot(
        collateral_token=collateral,
        instruments=instruments,
        metadata_hash=root_hash,
    )


def _parse_margin_summary(value: object, field: str) -> MarginSummary:
    root = _mapping(value, field)
    expected = {"accountValue", "totalNtlPos", "totalRawUsd", "totalMarginUsed"}
    if set(root) != expected:
        raise HyperliquidAccountResponseError(f"{field} fields are unsupported")
    return MarginSummary(
        account_value=_exact_decimal(root["accountValue"], f"{field}.accountValue"),
        total_notional_position=_exact_decimal(
            root["totalNtlPos"], f"{field}.totalNtlPos", nonnegative=True
        ),
        total_raw_usd=_exact_decimal(root["totalRawUsd"], f"{field}.totalRawUsd"),
        total_margin_used=_exact_decimal(
            root["totalMarginUsed"],
            f"{field}.totalMarginUsed",
            nonnegative=True,
        ),
    )


def _parse_position(
    value: object,
    index: int,
    metadata: PerpMetadataSnapshot,
) -> PerpPosition:
    wrapper = _mapping(value, f"clearinghouseState.assetPositions[{index}]")
    if set(wrapper) != {"type", "position"} or wrapper["type"] != "oneWay":
        raise HyperliquidAccountResponseError("asset position wrapper is unsupported")
    position = _mapping(wrapper["position"], f"assetPositions[{index}].position")
    expected = {
        "coin",
        "cumFunding",
        "entryPx",
        "leverage",
        "liquidationPx",
        "marginUsed",
        "maxLeverage",
        "positionValue",
        "returnOnEquity",
        "szi",
        "unrealizedPnl",
    }
    if set(position) != expected:
        raise HyperliquidAccountResponseError("position fields are unsupported")
    symbol = _symbol(position["coin"], f"assetPositions[{index}].position.coin")
    try:
        instrument = metadata.instrument(symbol)
    except ValidationError as error:
        raise HyperliquidAccountResponseError(
            f"position references unknown metadata symbol {symbol!r}"
        ) from error
    signed_size = _exact_decimal(position["szi"], f"position[{symbol}].szi")
    if signed_size == _ZERO:
        raise HyperliquidAccountResponseError("assetPositions must not contain zero positions")

    leverage = _mapping(position["leverage"], f"position[{symbol}].leverage")
    leverage_type = leverage.get("type")
    if leverage_type == "cross" and set(leverage) == {"type", "value"}:
        leverage_raw_usd = None
    elif leverage_type == "isolated" and set(leverage) == {"type", "value", "rawUsd"}:
        leverage_raw_usd = _exact_decimal(
            leverage["rawUsd"],
            f"position[{symbol}].leverage.rawUsd",
        )
    else:
        raise HyperliquidAccountResponseError("position leverage shape is unsupported")
    leverage_value = _exact_integer_decimal(
        leverage["value"],
        f"position[{symbol}].leverage.value",
        positive=True,
    )
    max_leverage = _exact_integer_decimal(
        position["maxLeverage"],
        f"position[{symbol}].maxLeverage",
        positive=True,
    )
    if leverage_value > max_leverage or max_leverage > instrument.max_leverage:
        raise HyperliquidAccountResponseError("position leverage exceeds fresh metadata")

    funding = _mapping(position["cumFunding"], f"position[{symbol}].cumFunding")
    if set(funding) != {"allTime", "sinceOpen", "sinceChange"}:
        raise HyperliquidAccountResponseError("position cumulative funding is unsupported")
    return PerpPosition(
        symbol=symbol,
        asset_id=instrument.asset_id,
        signed_size=signed_size,
        entry_price=_optional_decimal(
            position["entryPx"], f"position[{symbol}].entryPx", positive=True
        ),
        position_value=_exact_decimal(
            position["positionValue"],
            f"position[{symbol}].positionValue",
            nonnegative=True,
        ),
        unrealized_pnl=_exact_decimal(
            position["unrealizedPnl"], f"position[{symbol}].unrealizedPnl"
        ),
        margin_used=_exact_decimal(
            position["marginUsed"],
            f"position[{symbol}].marginUsed",
            nonnegative=True,
        ),
        liquidation_price=_optional_decimal(
            position["liquidationPx"],
            f"position[{symbol}].liquidationPx",
            positive=True,
        ),
        leverage_type=leverage_type,
        leverage=leverage_value,
        leverage_raw_usd=leverage_raw_usd,
        max_leverage=max_leverage,
        return_on_equity=_exact_decimal(
            position["returnOnEquity"], f"position[{symbol}].returnOnEquity"
        ),
        cumulative_funding_all_time=_exact_decimal(
            funding["allTime"], f"position[{symbol}].cumFunding.allTime"
        ),
        cumulative_funding_since_open=_exact_decimal(
            funding["sinceOpen"], f"position[{symbol}].cumFunding.sinceOpen"
        ),
        cumulative_funding_since_change=_exact_decimal(
            funding["sinceChange"], f"position[{symbol}].cumFunding.sinceChange"
        ),
    )


def _parse_clearinghouse(
    response: object,
    metadata: PerpMetadataSnapshot,
) -> tuple[
    int,
    MarginSummary,
    MarginSummary,
    Decimal,
    Decimal,
    tuple[PerpPosition, ...],
]:
    root = _mapping(response, "clearinghouseState response")
    expected = {
        "assetPositions",
        "crossMaintenanceMarginUsed",
        "crossMarginSummary",
        "marginSummary",
        "time",
        "withdrawable",
    }
    if set(root) != expected:
        raise HyperliquidAccountResponseError(
            "clearinghouseState fields are unsupported"
        )
    server_time = _integer(root["time"], "clearinghouseState.time")
    positions = tuple(
        sorted(
            (
                _parse_position(value, index, metadata)
                for index, value in enumerate(
                    _array(root["assetPositions"], "clearinghouseState.assetPositions")
                )
            ),
            key=lambda item: item.asset_id,
        )
    )
    if len({position.symbol.casefold() for position in positions}) != len(positions):
        raise HyperliquidAccountResponseError("duplicate position symbols are unsupported")
    return (
        server_time,
        _parse_margin_summary(root["marginSummary"], "marginSummary"),
        _parse_margin_summary(root["crossMarginSummary"], "crossMarginSummary"),
        _exact_decimal(
            root["crossMaintenanceMarginUsed"],
            "crossMaintenanceMarginUsed",
            nonnegative=True,
        ),
        _exact_decimal(root["withdrawable"], "withdrawable", nonnegative=True),
        positions,
    )


_ORDER_REQUIRED_FIELDS = {
    "coin",
    "isPositionTpsl",
    "isTrigger",
    "limitPx",
    "oid",
    "orderType",
    "origSz",
    "reduceOnly",
    "side",
    "sz",
    "timestamp",
    "triggerCondition",
    "triggerPx",
}
_ORDER_OPTIONAL_FIELDS = {"children", "cloid", "tif"}


def _trigger_kind(is_trigger: bool, order_type: str) -> TriggerKind | None:
    if not is_trigger:
        return None
    folded = order_type.casefold()
    if folded.startswith("stop"):
        return TriggerKind.STOP_LOSS
    if folded.startswith("take"):
        return TriggerKind.TAKE_PROFIT
    return TriggerKind.UNKNOWN


def _parse_open_order(
    value: object,
    field: str,
    metadata: PerpMetadataSnapshot,
    *,
    depth: int,
) -> OpenOrder:
    if depth > _MAX_ORDER_TREE_DEPTH:
        raise HyperliquidAccountResponseError("frontend order nesting is too deep")
    root = _mapping(value, field)
    if not _ORDER_REQUIRED_FIELDS.issubset(root) or not set(root).issubset(
        _ORDER_REQUIRED_FIELDS | _ORDER_OPTIONAL_FIELDS
    ):
        raise HyperliquidAccountResponseError(f"{field} fields are unsupported")
    symbol = _symbol(root["coin"], f"{field}.coin")
    try:
        instrument = metadata.instrument(symbol)
    except ValidationError as error:
        raise HyperliquidAccountResponseError(
            f"open order references unknown metadata symbol {symbol!r}"
        ) from error
    is_trigger = _bool(root["isTrigger"], f"{field}.isTrigger")
    reduce_only = _bool(root["reduceOnly"], f"{field}.reduceOnly")
    position_tpsl = _bool(root["isPositionTpsl"], f"{field}.isPositionTpsl")
    if is_trigger and not reduce_only:
        raise HyperliquidAccountResponseError("trigger orders must be reduce-only")
    if position_tpsl and not is_trigger:
        raise HyperliquidAccountResponseError("position TP/SL must be a trigger order")
    side_wire = root["side"]
    if side_wire == "B":
        side = OrderSide.BUY
    elif side_wire == "A":
        side = OrderSide.SELL
    else:
        raise HyperliquidAccountResponseError(f"{field}.side is unsupported")
    remaining = _exact_decimal(root["sz"], f"{field}.sz", nonnegative=True)
    original = _exact_decimal(
        root["origSz"], f"{field}.origSz", nonnegative=True
    )
    if remaining > original:
        raise HyperliquidAccountResponseError("open order remaining size exceeds original size")
    if original == _ZERO and not position_tpsl:
        raise HyperliquidAccountResponseError(
            "zero-size open orders must be position TP/SL orders"
        )
    trigger_price = _exact_decimal(
        root["triggerPx"], f"{field}.triggerPx", nonnegative=True
    )
    if is_trigger != (trigger_price > _ZERO):
        raise HyperliquidAccountResponseError("trigger flag and trigger price disagree")
    order_type = _text(root["orderType"], f"{field}.orderType", maximum=64)
    raw_children = root.get("children", [])
    children = tuple(
        sorted(
            (
                _parse_open_order(
                    child,
                    f"{field}.children[{index}]",
                    metadata,
                    depth=depth + 1,
                )
                for index, child in enumerate(
                    _array(raw_children, f"{field}.children", maximum=20)
                )
            ),
            key=lambda item: item.oid,
        )
    )
    raw_cloid = root.get("cloid")
    cloid = None if raw_cloid is None else _cloid(raw_cloid, f"{field}.cloid")
    raw_tif = root.get("tif")
    tif = None if raw_tif is None else _text(raw_tif, f"{field}.tif", maximum=64)
    return OpenOrder(
        symbol=symbol,
        asset_id=instrument.asset_id,
        oid=_integer(root["oid"], f"{field}.oid", maximum=2**63 - 1),
        cloid=cloid,
        side=side,
        remaining_size=remaining,
        original_size=original,
        limit_price=_exact_decimal(root["limitPx"], f"{field}.limitPx", positive=True),
        order_type=order_type,
        tif=tif,
        timestamp_ms=_integer(root["timestamp"], f"{field}.timestamp"),
        is_trigger=is_trigger,
        trigger_price=trigger_price,
        trigger_condition=_text(
            root["triggerCondition"], f"{field}.triggerCondition", maximum=128
        ),
        trigger_kind=_trigger_kind(is_trigger, order_type),
        reduce_only=reduce_only,
        is_position_tpsl=position_tpsl,
        children=children,
    )


def _parse_open_orders(
    response: object,
    metadata: PerpMetadataSnapshot,
    *,
    received_at_ms: int,
    future_skew_ms: int,
) -> tuple[OpenOrder, ...]:
    values = _array(response, "frontendOpenOrders response", maximum=_MAX_OPEN_ORDERS)
    orders = tuple(
        sorted(
            (
                _parse_open_order(
                    value,
                    f"frontendOpenOrders[{index}]",
                    metadata,
                    depth=0,
                )
                for index, value in enumerate(values)
            ),
            key=lambda item: item.oid,
        )
    )
    flattened = tuple(order for root in orders for order in root.walk())
    oids = [order.oid for order in flattened]
    if len(set(oids)) != len(oids):
        raise HyperliquidAccountResponseError("frontendOpenOrders contains duplicate oids")
    cloids = [order.cloid for order in flattened if order.cloid is not None]
    if len(set(cloids)) != len(cloids):
        raise HyperliquidAccountResponseError("frontendOpenOrders contains duplicate CLOIDs")
    if any(order.timestamp_ms > received_at_ms + future_skew_ms for order in flattened):
        raise HyperliquidAccountResponseError("open order timestamp is implausibly in the future")
    return orders


def _post_info(
    endpoint: str,
    payload: Mapping[str, object],
    transport: InfoTransport,
) -> object:
    try:
        return transport(endpoint, payload)
    except HyperliquidAccountError:
        raise
    except Exception as error:
        raise HyperliquidAccountTransportError(
            f"account info transport failed: {type(error).__name__}"
        ) from error


def fetch_account_snapshot(
    main_account_address: str,
    network: str,
    *,
    transport: InfoTransport = post_public_info,
    clock: Clock = lambda: datetime.now(timezone.utc),
    maximum_age_ms: int = 5_000,
    maximum_future_skew_ms: int = 5_000,
) -> HyperliquidAccountSnapshot:
    """Read and bind a fresh standard-mode account reconciliation snapshot."""

    account = _address(main_account_address)
    if not callable(transport):
        raise TypeError("transport must be callable")
    if not callable(clock):
        raise TypeError("clock must be callable")
    maximum_age = _request_integer(
        maximum_age_ms,
        "maximum_age_ms",
        minimum=1,
        maximum=300_000,
    )
    future_skew = _request_integer(
        maximum_future_skew_ms,
        "maximum_future_skew_ms",
        minimum=0,
        maximum=60_000,
    )
    endpoint = public_info_endpoint(network)

    mode_response = _post_info(
        endpoint,
        {"type": "userAbstraction", "user": account},
        transport,
    )
    mode = _parse_account_mode(mode_response)
    metadata = _parse_metadata(
        _post_info(endpoint, {"type": "meta"}, transport)
    )
    clearing_response = _post_info(
        endpoint,
        {"type": "clearinghouseState", "user": account},
        transport,
    )
    orders_response = _post_info(
        endpoint,
        {"type": "frontendOpenOrders", "user": account},
        transport,
    )
    received_at = _clock_read(clock)
    received_at_ms = _datetime_to_ms(received_at)
    (
        server_time,
        margin_summary,
        cross_margin_summary,
        cross_maintenance,
        withdrawable,
        positions,
    ) = _parse_clearinghouse(clearing_response, metadata)
    age_ms = received_at_ms - server_time
    if age_ms > maximum_age:
        raise StaleAccountSnapshotError(
            f"clearinghouseState is stale by {age_ms} milliseconds"
        )
    if age_ms < -future_skew:
        raise StaleAccountSnapshotError(
            "clearinghouseState time is implausibly in the future"
        )
    orders = _parse_open_orders(
        orders_response,
        metadata,
        received_at_ms=received_at_ms,
        future_skew_ms=future_skew,
    )

    material = {
        "schema_version": ACCOUNT_SNAPSHOT_SCHEMA_VERSION,
        "venue": "hyperliquid",
        "network": network,
        "main_account_address": account,
        "account_mode": mode.value,
        "server_time_ms": server_time,
        "margin_summary": margin_summary.canonical_record(),
        "cross_margin_summary": cross_margin_summary.canonical_record(),
        "cross_maintenance_margin_used": canonical_decimal(cross_maintenance),
        "withdrawable": canonical_decimal(withdrawable),
        "positions": [position.canonical_record() for position in positions],
        "open_orders": [order.canonical_record() for order in orders],
        "metadata_hash": metadata.metadata_hash,
    }
    digest = domain_hash(ACCOUNT_SNAPSHOT_HASH_DOMAIN, material)
    return HyperliquidAccountSnapshot(
        network=network,
        source_url=endpoint,
        main_account_address=account,
        account_mode=mode,
        server_time_ms=server_time,
        received_at_ms=received_at_ms,
        age_ms=age_ms,
        margin_summary=margin_summary,
        cross_margin_summary=cross_margin_summary,
        cross_maintenance_margin_used=cross_maintenance,
        withdrawable=withdrawable,
        positions=positions,
        open_orders=orders,
        metadata=metadata,
        snapshot_hash=digest,
    )


__all__ = (
    "ACCOUNT_SNAPSHOT_HASH_DOMAIN",
    "ACCOUNT_SNAPSHOT_SCHEMA_VERSION",
    "METADATA_SNAPSHOT_HASH_DOMAIN",
    "AccountReconciliation",
    "HyperliquidAccountError",
    "HyperliquidAccountResponseError",
    "HyperliquidAccountSnapshot",
    "HyperliquidAccountTransportError",
    "MarginSummary",
    "OpenOrder",
    "OrderSide",
    "PerpInstrument",
    "PerpMetadataSnapshot",
    "PerpPosition",
    "PositionSide",
    "ProtectionCoverage",
    "StaleAccountSnapshotError",
    "StandardAccountMode",
    "TriggerKind",
    "UnsupportedAccountModeError",
    "fetch_account_snapshot",
)
