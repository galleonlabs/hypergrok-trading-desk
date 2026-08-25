"""Bounded, read-only Hyperliquid TESTNET daily-loss synchronization.

The synchronizer deliberately has no signer, credential, or exchange-write
capability.  It reads only ``userFillsByTime`` and ``userFunding`` from the
compiled-in TESTNET ``/info`` endpoint, validates every returned field, and
then appends conservative economic events to :class:`DailyLossLedger`.

Both endpoints use inclusive time cursors.  A full page is therefore queried
again from its final timestamp so that records sharing a millisecond cannot be
silently skipped.  If that inclusive boundary cannot advance, the page budget
is exhausted, or Hyperliquid's fill-retention ceiling is reached, the stream
is explicitly incomplete and no coverage assertion is written for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException
import re
from typing import TypeAlias

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .daily_loss import DailyLossLedger, LossCoverageSource
from .domain import Environment
from .errors import HarnessError, ValidationError
from .market_data import post_public_info, public_info_endpoint


InfoTransport: TypeAlias = Callable[[str, Mapping[str, object]], object]
Clock: TypeAlias = Callable[[], datetime]

TESTNET_INFO_ENDPOINT = "https://api.hyperliquid-testnet.xyz/info"
USER_FILLS_PAGE_LIMIT = 2_000
USER_FILLS_RETENTION_LIMIT = 10_000
# Hyperliquid's general time-range pagination contract is 500 records or
# distinct blocks.  ``userFillsByTime`` documents its larger limit above.
USER_FUNDING_PAGE_LIMIT = 500
MAX_FILL_PAGES = 7
MAX_FUNDING_PAGES = 64

_FILL_REQUIRED_FIELDS = frozenset(
    {
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
)
_FILL_OPTIONAL_FIELDS = frozenset({"builderFee", "liquidation"})
_FUNDING_FIELDS = frozenset({"time", "hash", "delta"})
_FUNDING_DELTA_REQUIRED_FIELDS = frozenset(
    {"type", "coin", "fundingRate", "szi", "usdc"}
)
_FUNDING_DELTA_OPTIONAL_FIELDS = frozenset({"nSamples"})
_LIQUIDATION_REQUIRED_FIELDS = frozenset({"markPx", "method"})
_LIQUIDATION_OPTIONAL_FIELDS = frozenset({"liquidatedUser"})

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@_.:/-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
_ZERO = Decimal("0")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TIMESTAMP_MS = 253_402_300_799_999


class HyperliquidLossSyncError(HarnessError):
    """Base class for expected daily-loss synchronization failures."""


class HyperliquidLossSyncTransportError(HyperliquidLossSyncError):
    """The allowlisted public TESTNET read could not be completed."""


class HyperliquidLossSyncResponseError(HyperliquidLossSyncError, ValueError):
    """A venue response failed the strict expected schema."""


@dataclass(frozen=True, slots=True)
class LossStreamSync:
    source: LossCoverageSource
    requested_from: datetime
    requested_through: datetime
    page_count: int
    returned_rows: int
    unique_rows: int
    duplicate_rows: int
    complete: bool
    incomplete_reason: str | None
    inserted_events: int
    existing_events: int
    coverage_inserted: bool
    cursor_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "requested_from": _time_text(self.requested_from),
            "requested_through": _time_text(self.requested_through),
            "page_count": self.page_count,
            "returned_rows": self.returned_rows,
            "unique_rows": self.unique_rows,
            "duplicate_rows": self.duplicate_rows,
            "complete": self.complete,
            "incomplete_reason": self.incomplete_reason,
            "inserted_events": self.inserted_events,
            "existing_events": self.existing_events,
            "coverage_inserted": self.coverage_inserted,
            "cursor_hash": self.cursor_hash,
        }


@dataclass(frozen=True, slots=True)
class HyperliquidDailyLossSync:
    account_address_hash: str
    environment: Environment
    requested_from: datetime
    requested_through: datetime
    fills: LossStreamSync
    funding: LossStreamSync
    complete: bool
    report_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "account_address_hash": self.account_address_hash,
            "environment": self.environment.value,
            "requested_from": _time_text(self.requested_from),
            "requested_through": _time_text(self.requested_through),
            "fills": self.fills.as_dict(),
            "funding": self.funding.as_dict(),
            "complete": self.complete,
            "report_hash": self.report_hash,
        }


@dataclass(frozen=True, slots=True)
class _Fill:
    time_ms: int
    transaction_hash: str
    oid: int
    tid: int
    coin: str
    closed_pnl: Decimal
    fee: Decimal
    fee_token: str
    price: Decimal
    size: Decimal
    side: str
    start_position: Decimal
    crossed: bool
    direction: str
    builder_fee: Decimal | None
    liquidation: tuple[str, str, str | None] | None

    @property
    def identity(self) -> int:
        # Hyperliquid specifies ``tid`` as the unique trade id.  Keying on it
        # alone makes any malformed reuse conflict instead of creating a
        # second economic event.
        return self.tid

    @property
    def identity_material(self) -> dict[str, object]:
        return {
            "network": "testnet",
            "tid": self.tid,
        }

    @property
    def record_hash(self) -> str:
        return domain_hash("trading-harness/hyperliquid-loss-fill/v1", self)


@dataclass(frozen=True, slots=True)
class _Funding:
    time_ms: int
    transaction_hash: str
    coin: str
    funding_rate: Decimal
    signed_position: Decimal
    usdc: Decimal
    sample_count: int | None

    @property
    def identity(self) -> tuple[int, str, str]:
        return (self.time_ms, self.transaction_hash, self.coin)

    @property
    def identity_material(self) -> dict[str, object]:
        return {
            "network": "testnet",
            "time_ms": self.time_ms,
            "transaction_hash": self.transaction_hash,
            "coin": self.coin,
        }

    @property
    def record_hash(self) -> str:
        return domain_hash("trading-harness/hyperliquid-loss-funding/v1", self)


@dataclass(frozen=True, slots=True)
class _PageResult:
    records: tuple[_Fill, ...] | tuple[_Funding, ...]
    page_count: int
    returned_rows: int
    duplicate_rows: int
    complete: bool
    incomplete_reason: str | None


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _epoch_ms(value: datetime) -> int:
    delta = value - _EPOCH
    result = ((delta.days * 86_400 + delta.seconds) * 1_000) + (
        delta.microseconds // 1_000
    )
    if not 0 <= result <= _MAX_TIMESTAMP_MS:
        raise ValidationError("synchronization clock is outside the supported range")
    return result


def _from_epoch_ms(value: int) -> datetime:
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except (OverflowError, OSError, ValueError) as error:
        raise HyperliquidLossSyncResponseError("timestamp is outside supported range") from error


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise HyperliquidLossSyncResponseError(f"{field} must be a JSON object")
    return value


def _exact_keys(
    root: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    field: str,
) -> None:
    keys = set(root)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise HyperliquidLossSyncResponseError(f"{field} fields are unsupported")


def _array(value: object, *, field: str, limit: int) -> list[object]:
    if not isinstance(value, list):
        raise HyperliquidLossSyncResponseError(f"{field} must be a JSON array")
    if len(value) > limit:
        raise HyperliquidLossSyncResponseError(f"{field} exceeds the documented page limit")
    return value


def _integer(value: object, *, field: str, maximum: int = _MAX_TIMESTAMP_MS) -> int:
    if type(value) is not int:
        raise HyperliquidLossSyncResponseError(f"{field} must be an integer")
    if not 0 <= value <= maximum:
        raise HyperliquidLossSyncResponseError(f"{field} is outside supported bounds")
    return value


def _text(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise HyperliquidLossSyncResponseError(f"{field} must be bounded, trimmed text")
    return value


def _decimal(
    value: object,
    *,
    field: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
    ):
        raise HyperliquidLossSyncResponseError(
            f"{field} must be an exact decimal string"
        )
    try:
        parsed = Decimal(value)
        validate_decimal_bounds(parsed, field=field)
    except (DecimalException, ValueError, TypeError) as error:
        raise HyperliquidLossSyncResponseError(f"{field} is invalid") from error
    if positive and parsed <= _ZERO:
        raise HyperliquidLossSyncResponseError(f"{field} must be positive")
    if nonnegative and parsed < _ZERO:
        raise HyperliquidLossSyncResponseError(f"{field} must be non-negative")
    return parsed


def _symbol(value: object, *, field: str) -> str:
    parsed = _text(value, field=field, maximum=64)
    if not _SYMBOL_RE.fullmatch(parsed):
        raise HyperliquidLossSyncResponseError(f"{field} is invalid")
    return parsed


def _tx_hash(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _TX_HASH_RE.fullmatch(value):
        raise HyperliquidLossSyncResponseError(f"{field} is invalid")
    return value


def _parse_liquidation(value: object, *, field: str) -> tuple[str, str, str | None]:
    root = _mapping(value, field=field)
    _exact_keys(
        root,
        required=_LIQUIDATION_REQUIRED_FIELDS,
        optional=_LIQUIDATION_OPTIONAL_FIELDS,
        field=field,
    )
    mark = canonical_decimal(_decimal(root["markPx"], field=f"{field}.markPx", positive=True))
    method = root["method"]
    if method not in {"market", "backstop"}:
        raise HyperliquidLossSyncResponseError(f"{field}.method is unsupported")
    liquidated_user: str | None = None
    if "liquidatedUser" in root:
        candidate = root["liquidatedUser"]
        if not isinstance(candidate, str) or not _ADDRESS_RE.fullmatch(candidate):
            raise HyperliquidLossSyncResponseError(f"{field}.liquidatedUser is invalid")
        liquidated_user = candidate
    return (mark, method, liquidated_user)


def _parse_fill(
    value: object,
    *,
    field: str,
    page_start_ms: int,
    range_end_ms: int,
    settlement_currency: str,
) -> _Fill:
    root = _mapping(value, field=field)
    _exact_keys(
        root,
        required=_FILL_REQUIRED_FIELDS,
        optional=_FILL_OPTIONAL_FIELDS,
        field=field,
    )
    time_ms = _integer(root["time"], field=f"{field}.time")
    if not page_start_ms <= time_ms <= range_end_ms:
        raise HyperliquidLossSyncResponseError(f"{field}.time lies outside its request")
    transaction_hash = _tx_hash(root["hash"], field=f"{field}.hash")
    coin = _symbol(root["coin"], field=f"{field}.coin")
    crossed = root["crossed"]
    if type(crossed) is not bool:
        raise HyperliquidLossSyncResponseError(f"{field}.crossed must be boolean")
    direction = _text(root["dir"], field=f"{field}.dir", maximum=128)
    side = root["side"]
    if side not in {"B", "A"}:
        raise HyperliquidLossSyncResponseError(f"{field}.side is unsupported")
    fee_token = root["feeToken"]
    if not isinstance(fee_token, str) or not _TOKEN_RE.fullmatch(fee_token):
        raise HyperliquidLossSyncResponseError(f"{field}.feeToken is invalid")
    if fee_token != settlement_currency:
        raise HyperliquidLossSyncResponseError(
            f"{field}.feeToken is not the configured settlement currency"
        )
    fee = _decimal(root["fee"], field=f"{field}.fee", nonnegative=True)
    builder_fee: Decimal | None = None
    if "builderFee" in root:
        builder_fee = _decimal(
            root["builderFee"], field=f"{field}.builderFee", nonnegative=True
        )
        if builder_fee > fee:
            raise HyperliquidLossSyncResponseError(
                f"{field}.builderFee exceeds the total fee"
            )
    liquidation = None
    if "liquidation" in root:
        if root["liquidation"] is not None:
            liquidation = _parse_liquidation(
                root["liquidation"], field=f"{field}.liquidation"
            )
    return _Fill(
        time_ms=time_ms,
        transaction_hash=transaction_hash,
        oid=_integer(root["oid"], field=f"{field}.oid", maximum=2**63 - 1),
        tid=_integer(root["tid"], field=f"{field}.tid", maximum=2**63 - 1),
        coin=coin,
        closed_pnl=_decimal(root["closedPnl"], field=f"{field}.closedPnl"),
        fee=fee,
        fee_token=fee_token,
        price=_decimal(root["px"], field=f"{field}.px", positive=True),
        size=_decimal(root["sz"], field=f"{field}.sz", positive=True),
        side=side,
        start_position=_decimal(
            root["startPosition"], field=f"{field}.startPosition"
        ),
        crossed=crossed,
        direction=direction,
        builder_fee=builder_fee,
        liquidation=liquidation,
    )


def _parse_funding(
    value: object,
    *,
    field: str,
    page_start_ms: int,
    range_end_ms: int,
) -> _Funding:
    root = _mapping(value, field=field)
    _exact_keys(root, required=_FUNDING_FIELDS, field=field)
    time_ms = _integer(root["time"], field=f"{field}.time")
    if not page_start_ms <= time_ms <= range_end_ms:
        raise HyperliquidLossSyncResponseError(f"{field}.time lies outside its request")
    delta = _mapping(root["delta"], field=f"{field}.delta")
    _exact_keys(
        delta,
        required=_FUNDING_DELTA_REQUIRED_FIELDS,
        optional=_FUNDING_DELTA_OPTIONAL_FIELDS,
        field=f"{field}.delta",
    )
    if delta["type"] != "funding":
        raise HyperliquidLossSyncResponseError(f"{field}.delta.type is unsupported")
    sample_count: int | None = None
    if "nSamples" in delta and delta["nSamples"] is not None:
        sample_count = _integer(
            delta["nSamples"], field=f"{field}.delta.nSamples", maximum=10_000_000
        )
    return _Funding(
        time_ms=time_ms,
        transaction_hash=_tx_hash(root["hash"], field=f"{field}.hash"),
        coin=_symbol(delta["coin"], field=f"{field}.delta.coin"),
        funding_rate=_decimal(
            delta["fundingRate"], field=f"{field}.delta.fundingRate"
        ),
        signed_position=_decimal(delta["szi"], field=f"{field}.delta.szi"),
        usdc=_decimal(delta["usdc"], field=f"{field}.delta.usdc"),
        sample_count=sample_count,
    )


class HyperliquidDailyLossSynchronizer:
    """Synchronize one ledger-bound TESTNET main account."""

    def __init__(
        self,
        *,
        environment: Environment | str,
        account_id: str,
        main_account_address: str,
        config_hash: str,
        settlement_currency: str,
        ledger: DailyLossLedger,
        transport: InfoTransport = post_public_info,
        clock: Clock | None = None,
    ) -> None:
        if type(ledger) is not DailyLossLedger:
            raise TypeError("ledger must be exact DailyLossLedger")
        if not callable(transport):
            raise TypeError("transport must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        try:
            selected_environment = (
                environment
                if isinstance(environment, Environment)
                else Environment(environment)
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("daily-loss synchronization environment is invalid") from error
        if selected_environment is not Environment.TESTNET:
            raise ValidationError("daily-loss synchronization is TESTNET-only")
        if not isinstance(account_id, str) or account_id != ledger.binding.account_id:
            raise ValidationError("daily-loss account id differs from ledger binding")
        if not isinstance(config_hash, str) or not _HASH_RE.fullmatch(config_hash):
            raise ValidationError("daily-loss config hash is invalid")
        if not isinstance(settlement_currency, str) or not _TOKEN_RE.fullmatch(
            settlement_currency
        ):
            raise ValidationError("daily-loss settlement currency is invalid")
        binding = ledger.binding
        if (
            binding.environment is not Environment.TESTNET
            or binding.config_hash != config_hash
            or binding.settlement_currency != settlement_currency
        ):
            raise ValidationError(
                "daily-loss synchronizer configuration differs from ledger binding"
            )
        if not isinstance(main_account_address, str) or not _ADDRESS_RE.fullmatch(
            main_account_address
        ):
            raise ValidationError("configured main account address is invalid")
        endpoint = public_info_endpoint(selected_environment.value)
        if endpoint != TESTNET_INFO_ENDPOINT:
            raise ValidationError("refusing a non-TESTNET Hyperliquid endpoint")
        self._account_address = main_account_address
        self._settlement_currency = settlement_currency
        self._ledger = ledger
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._endpoint = endpoint

    def _post(self, payload: Mapping[str, object]) -> object:
        if self._endpoint != TESTNET_INFO_ENDPOINT:
            raise HyperliquidLossSyncTransportError("TESTNET endpoint binding changed")
        if payload.get("type") not in {"userFillsByTime", "userFunding"}:
            raise HyperliquidLossSyncTransportError("refusing an unsupported info read")
        try:
            return self._transport(self._endpoint, payload)
        except Exception as error:
            raise HyperliquidLossSyncTransportError(
                f"Hyperliquid TESTNET info read failed: {type(error).__name__}"
            ) from error

    def _fetch_fills(self, *, start_ms: int, end_ms: int) -> _PageResult:
        cursor = start_ms
        by_identity: dict[int, _Fill] = {}
        returned = 0
        duplicates = 0
        pages = 0
        complete = False
        reason: str | None = None
        required_overlap: set[int] = set()
        while pages < MAX_FILL_PAGES:
            rows = _array(
                self._post(
                    {
                        "type": "userFillsByTime",
                        "user": self._account_address,
                        "startTime": cursor,
                        "endTime": end_ms,
                        "aggregateByTime": False,
                    }
                ),
                field=f"userFillsByTime page {pages + 1}",
                limit=USER_FILLS_PAGE_LIMIT,
            )
            pages += 1
            returned += len(rows)
            parsed = tuple(
                _parse_fill(
                    value,
                    field=f"userFillsByTime[{index}]",
                    page_start_ms=cursor,
                    range_end_ms=end_ms,
                    settlement_currency=self._settlement_currency,
                )
                for index, value in enumerate(rows)
            )
            if any(
                parsed[index].time_ms > parsed[index + 1].time_ms
                for index in range(len(parsed) - 1)
            ):
                raise HyperliquidLossSyncResponseError(
                    "userFillsByTime page is not ordered by ascending time"
                )
            page_identities = {item.identity for item in parsed}
            if required_overlap and not required_overlap.issubset(page_identities):
                raise HyperliquidLossSyncResponseError(
                    "userFillsByTime inclusive page overlap is incomplete"
                )
            for item in parsed:
                previous = by_identity.get(item.identity)
                if previous is None:
                    by_identity[item.identity] = item
                elif previous == item:
                    duplicates += 1
                else:
                    raise HyperliquidLossSyncResponseError(
                        "duplicate fill identity has conflicting economics"
                    )
            if len(by_identity) >= USER_FILLS_RETENTION_LIMIT:
                reason = "latest_10000_fill_retention_limit"
                break
            if len(rows) < USER_FILLS_PAGE_LIMIT:
                complete = True
                break
            next_cursor = parsed[-1].time_ms
            if next_cursor <= cursor:
                reason = "inclusive_fill_boundary_saturated"
                break
            required_overlap = {
                item.identity for item in parsed if item.time_ms == next_cursor
            }
            cursor = next_cursor
        if not complete and reason is None:
            reason = "maximum_fill_pages_exhausted"
        ordered = tuple(by_identity[key] for key in sorted(by_identity))
        return _PageResult(ordered, pages, returned, duplicates, complete, reason)

    def _fetch_funding(self, *, start_ms: int, end_ms: int) -> _PageResult:
        cursor = start_ms
        by_identity: dict[tuple[int, str, str], _Funding] = {}
        returned = 0
        duplicates = 0
        pages = 0
        complete = False
        reason: str | None = None
        required_overlap: set[tuple[int, str, str]] = set()
        while pages < MAX_FUNDING_PAGES:
            rows = _array(
                self._post(
                    {
                        "type": "userFunding",
                        "user": self._account_address,
                        "startTime": cursor,
                        "endTime": end_ms,
                    }
                ),
                field=f"userFunding page {pages + 1}",
                limit=USER_FUNDING_PAGE_LIMIT,
            )
            pages += 1
            returned += len(rows)
            parsed = tuple(
                _parse_funding(
                    value,
                    field=f"userFunding[{index}]",
                    page_start_ms=cursor,
                    range_end_ms=end_ms,
                )
                for index, value in enumerate(rows)
            )
            if any(
                parsed[index].time_ms > parsed[index + 1].time_ms
                for index in range(len(parsed) - 1)
            ):
                raise HyperliquidLossSyncResponseError(
                    "userFunding page is not ordered by ascending time"
                )
            page_identities = {item.identity for item in parsed}
            if required_overlap and not required_overlap.issubset(page_identities):
                raise HyperliquidLossSyncResponseError(
                    "userFunding inclusive page overlap is incomplete"
                )
            for item in parsed:
                previous = by_identity.get(item.identity)
                if previous is None:
                    by_identity[item.identity] = item
                elif previous == item:
                    duplicates += 1
                else:
                    raise HyperliquidLossSyncResponseError(
                        "duplicate funding identity has conflicting economics"
                    )
            if len(rows) < USER_FUNDING_PAGE_LIMIT:
                complete = True
                break
            next_cursor = parsed[-1].time_ms
            if next_cursor <= cursor:
                reason = "inclusive_funding_boundary_saturated"
                break
            required_overlap = {
                item.identity for item in parsed if item.time_ms == next_cursor
            }
            cursor = next_cursor
        if not complete and reason is None:
            reason = "maximum_funding_pages_exhausted"
        ordered = tuple(by_identity[key] for key in sorted(by_identity))
        return _PageResult(ordered, pages, returned, duplicates, complete, reason)

    @staticmethod
    def _source_ref(kind: str, identity: Mapping[str, object]) -> str:
        digest = domain_hash(
            f"trading-harness/hyperliquid-loss-{kind}-identity/v1", identity
        )
        return f"hyperliquid:testnet:{kind}:{digest}"

    def _append_fills(
        self, result: _PageResult
    ) -> tuple[int, int]:
        inserted = 0
        existing = 0
        for value in result.records:
            if not isinstance(value, _Fill):
                raise TypeError("internal fills result contains another record type")
            identity_hash = domain_hash(
                "trading-harness/hyperliquid-loss-fill-identity/v1",
                value.identity_material,
            )
            source_ref = self._source_ref("fill", value.identity_material)
            occurred = _from_epoch_ms(value.time_ms)
            for was_inserted in (
                self._ledger.record_realized_pnl(
                    event_id=f"hl-fill-pnl-{identity_hash}",
                    source_ref=source_ref,
                    occurred_at=occurred,
                    realized_pnl=value.closed_pnl,
                ),
                self._ledger.record_fee(
                    event_id=f"hl-fill-fee-{identity_hash}",
                    source_ref=source_ref,
                    occurred_at=occurred,
                    fee=value.fee,
                ),
            ):
                if was_inserted:
                    inserted += 1
                else:
                    existing += 1
        return inserted, existing

    def _append_funding(
        self, result: _PageResult
    ) -> tuple[int, int]:
        inserted = 0
        existing = 0
        for value in result.records:
            if not isinstance(value, _Funding):
                raise TypeError("internal funding result contains another record type")
            identity_hash = domain_hash(
                "trading-harness/hyperliquid-loss-funding-identity/v1",
                value.identity_material,
            )
            was_inserted = self._ledger.record_funding(
                event_id=f"hl-funding-{identity_hash}",
                source_ref=self._source_ref("funding", value.identity_material),
                occurred_at=_from_epoch_ms(value.time_ms),
                net_funding=value.usdc,
            )
            if was_inserted:
                inserted += 1
            else:
                existing += 1
        return inserted, existing

    def _finish_stream(
        self,
        *,
        source: LossCoverageSource,
        result: _PageResult,
        start: datetime,
        through: datetime,
        inserted: int,
        existing: int,
    ) -> LossStreamSync:
        cursor_material = {
            "binding_hash": self._ledger.binding.binding_hash,
            "network": "testnet",
            "account_address_hash": domain_hash(
                "trading-harness/hyperliquid-account-address/v1",
                self._account_address,
            ),
            "source": source.value,
            "requested_from": start,
            "requested_through": through,
            "page_count": result.page_count,
            "returned_rows": result.returned_rows,
            "unique_rows": len(result.records),
            "duplicate_rows": result.duplicate_rows,
            "complete": result.complete,
            "incomplete_reason": result.incomplete_reason,
            "record_hashes": tuple(value.record_hash for value in result.records),
        }
        cursor_hash = domain_hash(
            "trading-harness/hyperliquid-loss-coverage-cursor/v1", cursor_material
        )
        coverage_inserted = False
        if result.complete:
            coverage_inserted = self._ledger.record_coverage(
                coverage_id=(
                    f"hl-{source.value}-{through:%Y%m%dT%H%M%S}-{cursor_hash[:24]}"
                ),
                source=source,
                covered_from=start,
                covered_through=through,
                source_cursor_hash=cursor_hash,
            )
        return LossStreamSync(
            source=source,
            requested_from=start,
            requested_through=through,
            page_count=result.page_count,
            returned_rows=result.returned_rows,
            unique_rows=len(result.records),
            duplicate_rows=result.duplicate_rows,
            complete=result.complete,
            incomplete_reason=result.incomplete_reason,
            inserted_events=inserted,
            existing_events=existing,
            coverage_inserted=coverage_inserted,
            cursor_hash=cursor_hash,
        )

    def synchronize(self) -> HyperliquidDailyLossSync:
        """Read today's two public streams and append only verified evidence."""

        try:
            clock_value = self._clock()
        except Exception as error:
            raise HyperliquidLossSyncError("daily-loss synchronization clock failed") from error
        observed = _utc(clock_value, field="clock")
        end_ms = _epoch_ms(observed)
        through = _from_epoch_ms(end_ms)
        start = datetime.combine(through.date(), time.min, tzinfo=timezone.utc)
        start_ms = _epoch_ms(start)

        # Fetch and validate both streams before performing any local writes.
        fills_result = self._fetch_fills(start_ms=start_ms, end_ms=end_ms)
        funding_result = self._fetch_funding(start_ms=start_ms, end_ms=end_ms)

        fills_inserted, fills_existing = self._append_fills(fills_result)
        funding_inserted, funding_existing = self._append_funding(funding_result)
        fills = self._finish_stream(
            source=LossCoverageSource.FILLS,
            result=fills_result,
            start=start,
            through=through,
            inserted=fills_inserted,
            existing=fills_existing,
        )
        funding = self._finish_stream(
            source=LossCoverageSource.FUNDING,
            result=funding_result,
            start=start,
            through=through,
            inserted=funding_inserted,
            existing=funding_existing,
        )
        account_hash = domain_hash(
            "trading-harness/hyperliquid-account-address/v1",
            self._account_address,
        )
        material = {
            "account_address_hash": account_hash,
            "environment": Environment.TESTNET,
            "requested_from": start,
            "requested_through": through,
            "fills": fills.as_dict(),
            "funding": funding.as_dict(),
            "complete": fills.complete and funding.complete,
        }
        return HyperliquidDailyLossSync(
            account_address_hash=account_hash,
            environment=Environment.TESTNET,
            requested_from=start,
            requested_through=through,
            fills=fills,
            funding=funding,
            complete=fills.complete and funding.complete,
            report_hash=domain_hash(
                "trading-harness/hyperliquid-daily-loss-sync/v1", material
            ),
        )


__all__ = (
    "MAX_FILL_PAGES",
    "MAX_FUNDING_PAGES",
    "TESTNET_INFO_ENDPOINT",
    "USER_FILLS_PAGE_LIMIT",
    "USER_FILLS_RETENTION_LIMIT",
    "USER_FUNDING_PAGE_LIMIT",
    "HyperliquidDailyLossSync",
    "HyperliquidDailyLossSynchronizer",
    "HyperliquidLossSyncError",
    "HyperliquidLossSyncResponseError",
    "HyperliquidLossSyncTransportError",
    "LossStreamSync",
)
