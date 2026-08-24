"""Strict, read-only ingestion of completed Hyperliquid candles.

The public ``candleSnapshot`` endpoint is convenient, but its output is not a
backtest artifact until its schema, time grid, coverage, and completion state
have been checked.  This module performs that trust-boundary work without any
account or exchange-write capability.

Hyperliquid treats both request boundaries as inclusive and returns at most
the most recent 5,000 candles.  Query boundaries are normalized to candle-open
times, exact duplicate boundary rows are collapsed, conflicting duplicates
and time-grid gaps are rejected, and a leading omission at the documented
5,000-row limit is surfaced as explicit truncation metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
import re
from types import MappingProxyType
from typing import Any, TypeAlias

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .errors import HarnessError, ValidationError
from .market_data import post_public_info, public_info_endpoint


HistoryTransport: TypeAlias = Callable[[str, Mapping[str, object]], object]
Clock: TypeAlias = Callable[[], datetime]

CANDLE_HISTORY_HASH_DOMAIN = "trading-harness/hyperliquid-candle-history/v1"
CANDLE_HISTORY_SCHEMA_VERSION = "hyperliquid.candle_history.v1"
HYPERLIQUID_CANDLE_LIMIT = 5_000

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_CANDLE_FIELDS = frozenset({"t", "T", "s", "i", "o", "c", "h", "l", "v", "n"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_TIMESTAMP_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z

# Hyperliquid's ``1M`` interval is a fixed 30-day grid, not a calendar month.
# Every duration below is therefore an exact millisecond grid duration.
_INTERVAL_DURATIONS_MS: Mapping[str, int] = MappingProxyType(
    {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "8h": 28_800_000,
        "12h": 43_200_000,
        "1d": 86_400_000,
        "3d": 259_200_000,
        "1w": 604_800_000,
        "1M": 2_592_000_000,
    }
)


class CandleHistoryError(HarnessError):
    """Base class for expected candle-history failures."""


class CandleHistoryTransportError(CandleHistoryError):
    """The allowlisted public endpoint could not be read."""


class CandleHistoryResponseError(CandleHistoryError, ValueError):
    """The venue response violated the candleSnapshot contract."""


class CandleHistoryGapError(CandleHistoryResponseError):
    """Required candle coverage contains an unexplained omission."""


class CandleHistoryConflictError(CandleHistoryResponseError):
    """One candle-open timestamp was returned with conflicting values."""


def supported_intervals() -> tuple[str, ...]:
    """Return the exact Hyperliquid candle interval allowlist."""

    return tuple(_INTERVAL_DURATIONS_MS)


def interval_duration_ms(interval: str) -> int:
    """Resolve an allowlisted interval to its exact grid duration."""

    if not isinstance(interval, str) or interval not in _INTERVAL_DURATIONS_MS:
        raise ValidationError(
            "interval must be one of: " + ", ".join(_INTERVAL_DURATIONS_MS)
        )
    return _INTERVAL_DURATIONS_MS[interval]


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CandleHistoryResponseError(f"{field} must be an integer")
    if value < minimum:
        raise CandleHistoryResponseError(f"{field} must be at least {minimum}")
    if value > _MAX_TIMESTAMP_MS and field.endswith((".t", ".T")):
        raise CandleHistoryResponseError(f"{field} exceeds the supported timestamp range")
    return value


def _request_timestamp(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer millisecond timestamp")
    if not 0 <= value <= _MAX_TIMESTAMP_MS:
        raise ValidationError(f"{field} is outside the supported timestamp range")
    return value


def _exact_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CandleHistoryResponseError(f"{field} must be an exact decimal string")
    try:
        result = Decimal(value)
        validate_decimal_bounds(result, field=field)
    except (DecimalException, ValueError) as error:
        raise CandleHistoryResponseError(
            f"{field} must be a bounded finite decimal string"
        ) from error
    if positive and result <= 0:
        raise CandleHistoryResponseError(f"{field} must be greater than zero")
    if nonnegative and result < 0:
        raise CandleHistoryResponseError(f"{field} must be non-negative")
    return result


def _utc_clock_read(clock: Clock) -> datetime:
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
    return _request_timestamp(result, "as_of_time_ms")


def _iso_from_ms(value: int) -> str:
    return (_EPOCH + timedelta(milliseconds=value)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _symbol(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not _SYMBOL_RE.fullmatch(value)
    ):
        raise ValidationError("symbol must be a canonical Hyperliquid symbol")
    return value


@dataclass(frozen=True, slots=True)
class HistoricalCandle:
    """One schema-checked Hyperliquid candle with exact numeric values."""

    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int

    def canonical_record(self) -> dict[str, str | int]:
        """Return the normalized venue record used by the data hash."""

        return {
            "t": self.open_time_ms,
            "T": self.close_time_ms,
            "s": self.symbol,
            "i": self.interval,
            "o": canonical_decimal(self.open),
            "h": canonical_decimal(self.high),
            "l": canonical_decimal(self.low),
            "c": canonical_decimal(self.close),
            "v": canonical_decimal(self.volume),
            "n": self.trade_count,
        }

    def to_technical_candle(self) -> Any:
        """Convert to the analysis module's completed-candle value object."""

        # Local import keeps history independent of indicator implementation
        # details and avoids making analysis part of this transport boundary.
        from .analysis import Candle

        return Candle(
            symbol=self.symbol,
            interval=self.interval,
            open_time=_EPOCH + timedelta(milliseconds=self.open_time_ms),
            close_time=_EPOCH + timedelta(milliseconds=self.close_time_ms),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


@dataclass(frozen=True, slots=True)
class CandleHistory:
    """A normalized candle series plus explicit coverage provenance."""

    network: str
    source_url: str
    symbol: str
    interval: str
    interval_ms: int
    requested_start_time_ms: int
    requested_end_time_ms: int
    query_start_open_time_ms: int
    query_end_open_time_ms: int
    as_of_time_ms: int
    expected_completed_candles: int
    response_count: int
    unique_response_count: int
    duplicate_count: int
    dropped_incomplete_count: int
    at_response_limit: bool
    truncated: bool
    truncation_reason: str | None
    coverage_complete: bool
    candles: tuple[HistoricalCandle, ...]
    data_hash: str

    def technical_candles(self) -> tuple[Any, ...]:
        """Return values ready for :func:`analysis.analyze_technical`."""

        return tuple(candle.to_technical_candle() for candle in self.candles)

    def as_dict(self) -> dict[str, object]:
        first = self.candles[0].open_time_ms if self.candles else None
        last = self.candles[-1].open_time_ms if self.candles else None
        return {
            "schema_version": CANDLE_HISTORY_SCHEMA_VERSION,
            "venue": "hyperliquid",
            "network": self.network,
            "symbol": self.symbol,
            "interval": self.interval,
            "interval_ms": self.interval_ms,
            "request": {
                "start_time_ms": self.requested_start_time_ms,
                "end_time_ms": self.requested_end_time_ms,
                "query_start_open_time_ms": self.query_start_open_time_ms,
                "query_end_open_time_ms": self.query_end_open_time_ms,
                "boundaries": "inclusive_candle_open_times",
            },
            "as_of_time_ms": self.as_of_time_ms,
            "as_of": _iso_from_ms(self.as_of_time_ms),
            "response": {
                "count": self.response_count,
                "unique_count": self.unique_response_count,
                "duplicate_count": self.duplicate_count,
                "dropped_incomplete_count": self.dropped_incomplete_count,
                "limit": HYPERLIQUID_CANDLE_LIMIT,
                "at_limit": self.at_response_limit,
            },
            "coverage": {
                "expected_completed_candles": self.expected_completed_candles,
                "completed_candles": len(self.candles),
                "first_open_time_ms": first,
                "last_open_time_ms": last,
                "complete": self.coverage_complete,
                "truncated": self.truncated,
                "truncation_reason": self.truncation_reason,
            },
            "data_hash_domain": CANDLE_HISTORY_HASH_DOMAIN,
            "data_hash": self.data_hash,
            "candles": [candle.canonical_record() for candle in self.candles],
            "source": {
                "url": self.source_url,
                "endpoint": "/info",
                "request_type": "candleSnapshot",
            },
        }


def _parse_candle(
    value: object,
    index: int,
    *,
    symbol: str,
    interval: str,
    duration_ms: int,
) -> HistoricalCandle:
    field = f"candleSnapshot[{index}]"
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CandleHistoryResponseError(f"{field} must be a JSON object")
    keys = frozenset(value)
    if keys != _CANDLE_FIELDS:
        missing = sorted(_CANDLE_FIELDS - keys)
        unexpected = sorted(keys - _CANDLE_FIELDS)
        raise CandleHistoryResponseError(
            f"{field} fields are invalid; missing={missing!r}, unexpected={unexpected!r}"
        )

    open_time = _integer(value["t"], f"{field}.t")
    close_time = _integer(value["T"], f"{field}.T")
    if open_time % duration_ms != 0:
        raise CandleHistoryResponseError(f"{field}.t is not aligned to {interval}")
    if close_time != open_time + duration_ms - 1:
        raise CandleHistoryResponseError(
            f"{field}.T does not close the documented {interval} interval"
        )
    if value["s"] != symbol:
        raise CandleHistoryResponseError(f"{field}.s does not match the requested symbol")
    if value["i"] != interval:
        raise CandleHistoryResponseError(f"{field}.i does not match the requested interval")

    opened = _exact_decimal(value["o"], f"{field}.o", positive=True)
    closed = _exact_decimal(value["c"], f"{field}.c", positive=True)
    high = _exact_decimal(value["h"], f"{field}.h", positive=True)
    low = _exact_decimal(value["l"], f"{field}.l", positive=True)
    volume = _exact_decimal(value["v"], f"{field}.v", nonnegative=True)
    trade_count = _integer(value["n"], f"{field}.n")
    if high < max(opened, closed, low):
        raise CandleHistoryResponseError(f"{field}.h is below another OHLC value")
    if low > min(opened, closed, high):
        raise CandleHistoryResponseError(f"{field}.l is above another OHLC value")

    return HistoricalCandle(
        symbol=symbol,
        interval=interval,
        open_time_ms=open_time,
        close_time_ms=close_time,
        open=opened,
        high=high,
        low=low,
        close=closed,
        volume=volume,
        trade_count=trade_count,
    )


def _post_candles(
    endpoint: str,
    payload: Mapping[str, object],
    transport: HistoryTransport,
) -> object:
    try:
        return transport(endpoint, payload)
    except HarnessError:
        raise
    except Exception as error:
        raise CandleHistoryTransportError(
            f"candle-history transport failed: {type(error).__name__}"
        ) from error


def fetch_candle_history(
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    network: str,
    *,
    transport: HistoryTransport = post_public_info,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> CandleHistory:
    """Fetch and strictly normalize one Hyperliquid candleSnapshot response.

    ``start_time_ms`` and ``end_time_ms`` are inclusive.  If either falls
    inside a candle it is normalized down to that candle's open time, matching
    the venue's observed behavior.  ``end_time_ms`` may equal the current
    instant but must not be in the future.  A returned current candle is
    counted and dropped; a candle whose open lies in the future is rejected.

    A contiguous tail returned at the 5,000-row cap is returned with
    ``coverage_complete=False`` and ``truncated=True``.  Every other missing
    required candle is a hard :class:`CandleHistoryGapError`.
    """

    checked_symbol = _symbol(symbol)
    duration_ms = interval_duration_ms(interval)
    checked_start = _request_timestamp(start_time_ms, "start_time_ms")
    checked_end = _request_timestamp(end_time_ms, "end_time_ms")
    if checked_end < checked_start:
        raise ValidationError("end_time_ms must not precede start_time_ms")
    if not callable(transport):
        raise TypeError("transport must be callable")
    if not callable(clock):
        raise TypeError("clock must be callable")

    as_of = _utc_clock_read(clock)
    as_of_ms = _datetime_to_ms(as_of)
    if checked_end > as_of_ms:
        raise ValidationError("end_time_ms must not be in the future")

    query_start = checked_start - checked_start % duration_ms
    query_end = checked_end - checked_end % duration_ms
    endpoint = public_info_endpoint(network)
    payload: dict[str, object] = {
        "type": "candleSnapshot",
        "req": {
            "coin": checked_symbol,
            "interval": interval,
            "startTime": query_start,
            "endTime": query_end,
        },
    }
    response = _post_candles(endpoint, payload, transport)
    if not isinstance(response, list):
        raise CandleHistoryResponseError(
            "candleSnapshot response must be a JSON array"
        )
    response_count = len(response)
    if response_count > HYPERLIQUID_CANDLE_LIMIT:
        raise CandleHistoryResponseError(
            f"candleSnapshot response exceeds {HYPERLIQUID_CANDLE_LIMIT} candles"
        )

    by_open: dict[int, HistoricalCandle] = {}
    duplicate_count = 0
    for index, raw in enumerate(response):
        candle = _parse_candle(
            raw,
            index,
            symbol=checked_symbol,
            interval=interval,
            duration_ms=duration_ms,
        )
        if not query_start <= candle.open_time_ms <= query_end:
            raise CandleHistoryResponseError(
                f"candleSnapshot[{index}].t is outside the inclusive query range"
            )
        if candle.open_time_ms > as_of_ms:
            raise CandleHistoryResponseError(
                f"candleSnapshot[{index}].t is in the future"
            )
        previous = by_open.get(candle.open_time_ms)
        if previous is None:
            by_open[candle.open_time_ms] = candle
        elif previous == candle:
            duplicate_count += 1
        else:
            raise CandleHistoryConflictError(
                f"conflicting candles at open time {candle.open_time_ms}"
            )

    ordered = [by_open[key] for key in sorted(by_open)]
    current_open = as_of_ms - as_of_ms % duration_ms
    completed: list[HistoricalCandle] = []
    dropped_incomplete = 0
    for candle in ordered:
        if candle.close_time_ms < as_of_ms:
            completed.append(candle)
            continue
        if candle.open_time_ms != current_open:
            raise CandleHistoryResponseError(
                "only the current trailing candle may be incomplete"
            )
        dropped_incomplete += 1

    for left, right in zip(completed, completed[1:]):
        if right.open_time_ms != left.open_time_ms + duration_ms:
            raise CandleHistoryGapError(
                f"candle gap between {left.open_time_ms} and {right.open_time_ms}"
            )

    latest_completed_open = current_open - duration_ms
    expected_last = min(query_end, latest_completed_open)
    expected_count = (
        0
        if expected_last < query_start
        else (expected_last - query_start) // duration_ms + 1
    )
    at_limit = response_count == HYPERLIQUID_CANDLE_LIMIT
    truncated = False
    coverage_complete = expected_count == 0

    if expected_count == 0:
        if completed:
            raise CandleHistoryResponseError(
                "response contains a completed candle outside expected coverage"
            )
    elif not completed:
        raise CandleHistoryGapError("candleSnapshot omitted all required candles")
    else:
        if completed[-1].open_time_ms != expected_last:
            raise CandleHistoryGapError(
                "candleSnapshot does not reach the required completed end boundary"
            )
        if completed[0].open_time_ms == query_start:
            if len(completed) != expected_count:
                # Adjacency and endpoints were already checked, so this is a
                # defensive guard against arithmetic or future schema drift.
                raise CandleHistoryGapError("completed candle coverage is inconsistent")
            coverage_complete = True
        elif at_limit and duplicate_count == 0:
            truncated = True
            coverage_complete = False
        else:
            raise CandleHistoryGapError(
                "candleSnapshot omitted the required completed start boundary"
            )

    candles = tuple(completed)
    data_material = {
        "schema_version": CANDLE_HISTORY_SCHEMA_VERSION,
        "venue": "hyperliquid",
        "network": network,
        "symbol": checked_symbol,
        "interval": interval,
        "candles": [candle.canonical_record() for candle in candles],
    }
    digest = domain_hash(CANDLE_HISTORY_HASH_DOMAIN, data_material)
    return CandleHistory(
        network=network,
        source_url=endpoint,
        symbol=checked_symbol,
        interval=interval,
        interval_ms=duration_ms,
        requested_start_time_ms=checked_start,
        requested_end_time_ms=checked_end,
        query_start_open_time_ms=query_start,
        query_end_open_time_ms=query_end,
        as_of_time_ms=as_of_ms,
        expected_completed_candles=expected_count,
        response_count=response_count,
        unique_response_count=len(by_open),
        duplicate_count=duplicate_count,
        dropped_incomplete_count=dropped_incomplete,
        at_response_limit=at_limit,
        truncated=truncated,
        truncation_reason="hyperliquid_5000_candle_limit" if truncated else None,
        coverage_complete=coverage_complete,
        candles=candles,
        data_hash=digest,
    )


__all__ = (
    "CANDLE_HISTORY_HASH_DOMAIN",
    "CANDLE_HISTORY_SCHEMA_VERSION",
    "HYPERLIQUID_CANDLE_LIMIT",
    "CandleHistory",
    "CandleHistoryConflictError",
    "CandleHistoryError",
    "CandleHistoryGapError",
    "CandleHistoryResponseError",
    "CandleHistoryTransportError",
    "HistoricalCandle",
    "fetch_candle_history",
    "interval_duration_ms",
    "supported_intervals",
)
