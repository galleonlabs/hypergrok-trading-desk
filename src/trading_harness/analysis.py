"""Deterministic completed-candle technical analysis.

The calculations in this module are research evidence.  They never authorize
or submit an order.  All arithmetic uses a harness-owned Decimal context so a
host application's ambient precision cannot alter a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    Context,
    Decimal,
    DecimalException,
    ROUND_HALF_EVEN,
    localcontext,
)
from enum import Enum
import hashlib
from typing import Any, Iterable

from .canonical import canonical_decimal, canonical_json, validate_decimal_bounds
from .errors import ValidationError
from .policy import exact_decimal


_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")
_CONTEXT = Context(
    prec=96,
    rounding=ROUND_HALF_EVEN,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a non-empty, trimmed string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _positive(value: Decimal | str | int, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed <= _ZERO:
        raise ValidationError(f"{field} must be greater than zero")
    return parsed


def _nonnegative(value: Decimal | str | int, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed < _ZERO:
        raise ValidationError(f"{field} must not be negative")
    return parsed


def _checked(value: Decimal, field: str) -> Decimal:
    try:
        return validate_decimal_bounds(value, field=field)
    except ValueError as error:
        raise ValidationError(str(error)) from error


def _decimal_text(value: Decimal) -> str:
    return canonical_decimal(_checked(value, "technical value"))


class TechnicalBias(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NOTHING = "nothing"


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol", maximum=64))
        object.__setattr__(self, "interval", _text(self.interval, "interval", maximum=16))
        opened = _utc(self.open_time, "open_time")
        closed = _utc(self.close_time, "close_time")
        if closed <= opened:
            raise ValidationError("close_time must be after open_time")
        object.__setattr__(self, "open_time", opened)
        object.__setattr__(self, "close_time", closed)
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        object.__setattr__(self, "volume", _nonnegative(self.volume, "volume"))
        if self.high < max(self.open, self.close, self.low):
            raise ValidationError("high must be the candle maximum")
        if self.low > min(self.open, self.close, self.high):
            raise ValidationError("low must be the candle minimum")

    def canonical_record(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "close_time": self.close_time.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
        }


@dataclass(frozen=True, slots=True)
class TechnicalConfig:
    version: str = "trend-rsi-atr-v1"
    fast_period: int = 20
    slow_period: int = 50
    trend_period: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    rsi_buy_min: Decimal = Decimal("52")
    rsi_buy_max: Decimal = Decimal("70")
    rsi_sell_min: Decimal = Decimal("30")
    rsi_sell_max: Decimal = Decimal("48")
    stop_atr_multiple: Decimal = Decimal("2")
    reward_risk_multiple: Decimal = Decimal("3")

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version", maximum=64))
        for field in (
            "fast_period",
            "slow_period",
            "trend_period",
            "rsi_period",
            "atr_period",
        ):
            value = getattr(self, field)
            if type(value) is not int or value < 2 or value > 10_000:
                raise ValidationError(f"{field} must be an integer from 2 to 10000")
        if not self.fast_period < self.slow_period < self.trend_period:
            raise ValidationError("periods must satisfy fast < slow < trend")
        for field in (
            "rsi_buy_min",
            "rsi_buy_max",
            "rsi_sell_min",
            "rsi_sell_max",
        ):
            value = _nonnegative(getattr(self, field), field)
            if value > _ONE_HUNDRED:
                raise ValidationError(f"{field} must not exceed 100")
            object.__setattr__(self, field, value)
        if not self.rsi_sell_min <= self.rsi_sell_max < self.rsi_buy_min <= self.rsi_buy_max:
            raise ValidationError("RSI sell and buy bands must be ordered and non-overlapping")
        object.__setattr__(
            self,
            "stop_atr_multiple",
            _positive(self.stop_atr_multiple, "stop_atr_multiple"),
        )
        object.__setattr__(
            self,
            "reward_risk_multiple",
            _positive(self.reward_risk_multiple, "reward_risk_multiple"),
        )

    @property
    def minimum_candles(self) -> int:
        return max(
            self.trend_period,
            self.rsi_period + 1,
            self.atr_period + 1,
        )

    def canonical_record(self) -> dict[str, str | int]:
        return {
            "version": self.version,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "trend_period": self.trend_period,
            "rsi_period": self.rsi_period,
            "atr_period": self.atr_period,
            "rsi_buy_min": _decimal_text(self.rsi_buy_min),
            "rsi_buy_max": _decimal_text(self.rsi_buy_max),
            "rsi_sell_min": _decimal_text(self.rsi_sell_min),
            "rsi_sell_max": _decimal_text(self.rsi_sell_max),
            "stop_atr_multiple": _decimal_text(self.stop_atr_multiple),
            "reward_risk_multiple": _decimal_text(self.reward_risk_multiple),
        }


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    symbol: str
    interval: str
    as_of: datetime
    candle_close_time: datetime
    config_version: str
    config_hash: str
    data_hash: str
    completed_candles: int
    ignored_incomplete_candles: int
    close: Decimal
    ema_fast: Decimal
    ema_slow: Decimal
    ema_trend: Decimal
    rsi: Decimal
    atr: Decimal
    bias: TechnicalBias
    stop_price: Decimal | None
    target_price: Decimal | None
    reasons: tuple[str, ...]
    executable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "technical_snapshot.v1",
            "symbol": self.symbol,
            "interval": self.interval,
            "as_of": self.as_of.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "candle_close_time": self.candle_close_time.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "config_version": self.config_version,
            "config_hash": self.config_hash,
            "data_hash": self.data_hash,
            "completed_candles": self.completed_candles,
            "ignored_incomplete_candles": self.ignored_incomplete_candles,
            "close": _decimal_text(self.close),
            "ema_fast": _decimal_text(self.ema_fast),
            "ema_slow": _decimal_text(self.ema_slow),
            "ema_trend": _decimal_text(self.ema_trend),
            "rsi": _decimal_text(self.rsi),
            "atr": _decimal_text(self.atr),
            "bias": self.bias.value,
            "stop_price": None if self.stop_price is None else _decimal_text(self.stop_price),
            "target_price": (
                None if self.target_price is None else _decimal_text(self.target_price)
            ),
            "reasons": list(self.reasons),
            "executable": self.executable,
            "evidence_status": "research_candidate",
        }


def _ema(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise ValidationError("insufficient values for EMA")
    with localcontext(_CONTEXT) as context:
        seed = context.divide(sum(values[:period], _ZERO), Decimal(period))
        multiplier = context.divide(Decimal(2), Decimal(period + 1))
        result = seed
        for value in values[period:]:
            result = context.add(
                result,
                context.multiply(context.subtract(value, result), multiplier),
            )
    return _checked(result, "EMA")


def _rsi(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period + 1:
        raise ValidationError("insufficient closes for RSI")
    with localcontext(_CONTEXT) as context:
        changes = [context.subtract(right, left) for left, right in zip(values, values[1:])]
        gains = [max(change, _ZERO) for change in changes]
        losses = [max(-change, _ZERO) for change in changes]
        average_gain = context.divide(sum(gains[:period], _ZERO), Decimal(period))
        average_loss = context.divide(sum(losses[:period], _ZERO), Decimal(period))
        for gain, loss in zip(gains[period:], losses[period:]):
            average_gain = context.divide(
                context.add(context.multiply(average_gain, Decimal(period - 1)), gain),
                Decimal(period),
            )
            average_loss = context.divide(
                context.add(context.multiply(average_loss, Decimal(period - 1)), loss),
                Decimal(period),
            )
        if average_gain == _ZERO and average_loss == _ZERO:
            result = Decimal("50")
        elif average_loss == _ZERO:
            result = _ONE_HUNDRED
        elif average_gain == _ZERO:
            result = _ZERO
        else:
            relative_strength = context.divide(average_gain, average_loss)
            result = context.subtract(
                _ONE_HUNDRED,
                context.divide(_ONE_HUNDRED, context.add(Decimal(1), relative_strength)),
            )
    return _checked(result, "RSI")


def _atr(candles: list[Candle], period: int) -> Decimal:
    if len(candles) < period + 1:
        raise ValidationError("insufficient candles for ATR")
    with localcontext(_CONTEXT) as context:
        ranges: list[Decimal] = []
        for previous, current in zip(candles, candles[1:]):
            ranges.append(
                max(
                    context.subtract(current.high, current.low),
                    abs(context.subtract(current.high, previous.close)),
                    abs(context.subtract(current.low, previous.close)),
                )
            )
        result = context.divide(sum(ranges[:period], _ZERO), Decimal(period))
        for true_range in ranges[period:]:
            result = context.divide(
                context.add(
                    context.multiply(result, Decimal(period - 1)),
                    true_range,
                ),
                Decimal(period),
            )
    return _checked(result, "ATR")


def _hash_records(records: object) -> str:
    encoded = canonical_json(records).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def analyze_technical(
    candles: Iterable[Candle],
    *,
    as_of: datetime,
    config: TechnicalConfig = TechnicalConfig(),
) -> TechnicalSnapshot:
    """Analyze completed candles and return a non-executable research snapshot."""

    if not isinstance(config, TechnicalConfig):
        raise TypeError("config must be TechnicalConfig")
    checked_as_of = _utc(as_of, "as_of")
    values = list(candles)
    if not values or any(not isinstance(candle, Candle) for candle in values):
        raise ValidationError("candles must contain Candle records")
    for left, right in zip(values, values[1:]):
        if right.open_time <= left.open_time:
            raise ValidationError("candles must be strictly ordered without duplicates")
    symbol = values[0].symbol
    interval = values[0].interval
    if any(candle.symbol != symbol or candle.interval != interval for candle in values):
        raise ValidationError("all candles must use the same symbol and interval")
    completed = [candle for candle in values if candle.close_time <= checked_as_of]
    if any(candle.close_time > checked_as_of for candle in values[: len(completed)]):
        raise ValidationError("incomplete candles may appear only after completed candles")
    if len(completed) < config.minimum_candles:
        raise ValidationError(
            f"at least {config.minimum_candles} completed candles are required"
        )

    closes = [candle.close for candle in completed]
    ema_fast = _ema(closes, config.fast_period)
    ema_slow = _ema(closes, config.slow_period)
    ema_trend = _ema(closes, config.trend_period)
    rsi = _rsi(closes, config.rsi_period)
    atr = _atr(completed, config.atr_period)
    close = completed[-1].close

    buy_conditions = (
        close > ema_trend,
        ema_fast > ema_slow,
        config.rsi_buy_min <= rsi <= config.rsi_buy_max,
    )
    sell_conditions = (
        close < ema_trend,
        ema_fast < ema_slow,
        config.rsi_sell_min <= rsi <= config.rsi_sell_max,
    )
    reasons: list[str] = []
    bias = TechnicalBias.NOTHING
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    try:
        with localcontext(_CONTEXT) as context:
            stop_distance = context.multiply(atr, config.stop_atr_multiple)
            target_distance = context.multiply(
                stop_distance,
                config.reward_risk_multiple,
            )
            if all(buy_conditions) and atr > _ZERO:
                candidate_stop = context.subtract(close, stop_distance)
                if candidate_stop > _ZERO:
                    bias = TechnicalBias.BUY
                    stop_price = _checked(candidate_stop, "buy stop")
                    target_price = _checked(
                        context.add(close, target_distance), "buy target"
                    )
                    reasons.extend(("close_above_trend", "fast_above_slow", "rsi_buy_band"))
            elif all(sell_conditions) and atr > _ZERO:
                bias = TechnicalBias.SELL
                stop_price = _checked(context.add(close, stop_distance), "sell stop")
                target_price = _checked(
                    context.subtract(close, target_distance), "sell target"
                )
                if target_price <= _ZERO:
                    bias = TechnicalBias.NOTHING
                    stop_price = None
                    target_price = None
                else:
                    reasons.extend(("close_below_trend", "fast_below_slow", "rsi_sell_band"))
    except DecimalException as error:
        raise ValidationError("technical stop/target calculation failed") from error

    if bias is TechnicalBias.NOTHING:
        reasons.append("technical_conditions_not_aligned")
        if atr == _ZERO:
            reasons.append("atr_zero")

    config_record = config.canonical_record()
    data_records = [candle.canonical_record() for candle in completed]
    return TechnicalSnapshot(
        symbol=symbol,
        interval=interval,
        as_of=checked_as_of,
        candle_close_time=completed[-1].close_time,
        config_version=config.version,
        config_hash=_hash_records(config_record),
        data_hash=_hash_records(data_records),
        completed_candles=len(completed),
        ignored_incomplete_candles=len(values) - len(completed),
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_trend=ema_trend,
        rsi=rsi,
        atr=atr,
        bias=bias,
        stop_price=stop_price,
        target_price=target_price,
        reasons=tuple(reasons),
    )


__all__ = (
    "Candle",
    "TechnicalBias",
    "TechnicalConfig",
    "TechnicalSnapshot",
    "analyze_technical",
)
