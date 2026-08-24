"""Explicit bridges from venue candle artifacts to the registered strategy.

Hyperliquid encodes candle ``T`` as the final inclusive millisecond, while the
registered strategy models the close as the next bar's exclusive boundary.
Keeping that conversion here avoids a quiet one-millisecond gap and forces a
caller to distinguish historical observability from live receipt time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from .errors import ValidationError
from .history import CandleHistory, HistoricalCandle
from .strategy import Candle


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _time(milliseconds: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=milliseconds)


def _convert(candle: HistoricalCandle, *, received_at: datetime) -> Candle:
    return Candle(
        instrument=candle.symbol,
        interval=candle.interval,
        open_time=_time(candle.open_time_ms),
        close_time=_time(candle.close_time_ms + 1),
        received_at=received_at,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        complete=True,
    )


def backtest_candles(history: CandleHistory) -> tuple[Candle, ...]:
    """Create offline candles using each completed bar's first observable time.

    This is valid only for market OHLCV backtests.  It does not claim the
    dataset was actually ingested at each historical close, and it must not be
    used as a prospective-shadow receipt record.
    """

    if not isinstance(history, CandleHistory):
        raise TypeError("history must be CandleHistory")
    if not history.coverage_complete or history.truncated:
        raise ValidationError("backtest history must have complete requested coverage")
    if history.interval != "4h":
        raise ValidationError("candidate-v0 backtests require 4h history")
    return tuple(
        _convert(candle, received_at=_time(candle.close_time_ms + 1))
        for candle in history.candles
    )


def live_scan_candles(
    history: CandleHistory,
    *,
    receipt_times: Mapping[int, datetime] | None = None,
) -> tuple[Candle, ...]:
    """Create live-scan candles with durable first-receipt instants.

    A caller without a durable receipt map gets the history artifact's current
    retrieval time.  The always-on node supplies the original per-open-time
    receipts so polling the same completed bar cannot manufacture a new signal
    identity or reset its expiry.
    """

    if not isinstance(history, CandleHistory):
        raise TypeError("history must be CandleHistory")
    if not history.coverage_complete or history.truncated:
        raise ValidationError("live scan history must have complete requested coverage")
    if history.interval != "4h":
        raise ValidationError("candidate-v0 live scans require 4h history")
    fallback = _time(history.as_of_time_ms)
    checked_receipts: Mapping[int, datetime] = {} if receipt_times is None else receipt_times
    if not isinstance(checked_receipts, Mapping):
        raise TypeError("receipt_times must be a mapping or None")
    converted: list[Candle] = []
    for candle in history.candles:
        supplied = checked_receipts.get(candle.open_time_ms, fallback)
        if not isinstance(supplied, datetime) or supplied.tzinfo is None or supplied.utcoffset() is None:
            raise ValidationError("receipt_times values must be timezone-aware datetimes")
        close = _time(candle.close_time_ms + 1)
        received = supplied.astimezone(timezone.utc)
        if received < close:
            raise ValidationError("durable receipt time cannot predate candle completion")
        converted.append(_convert(candle, received_at=received))
    return tuple(converted)


__all__ = ("backtest_candles", "live_scan_candles")
