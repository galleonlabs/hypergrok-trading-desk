"""Frozen, deterministic four-hour trend-breakout research strategy.

This module is intentionally a scanner, not an execution system.  It accepts
only complete, contiguous four-hour candles and evaluates the registered
``candidate-v0`` rule from information observable at each candle close.  It
does not read a venue, select parameters, size a position, or authorize a
trade.

The registered rule is deliberately small:

* EMA(50) / EMA(200) trend and fast-EMA slope;
* a Donchian(20) close breakout *transition*, with the signal candle excluded
  from both the current and previous channel boundaries; and
* Wilder ATR(14), used later by the backtester for its frozen exits.

All calculations run under a module-owned :class:`~decimal.Context`; ambient
thread decimal settings cannot change a signal.  Binary floats are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from enum import Enum
from typing import Iterable, Sequence

from .canonical import domain_hash, validate_decimal_bounds


STRATEGY_HASH_DOMAIN = "trading-harness/registered-strategy/v1"
CANDLE_CHAIN_HASH_DOMAIN = "trading-harness/candle-chain/v1"
SIGNAL_HASH_DOMAIN = "trading-harness/strategy-signal/v1"
FOUR_HOURS = timedelta(hours=4)

_CALCULATION_CONTEXT = Context(
    prec=64,
    rounding=ROUND_HALF_EVEN,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)


class StrategyDataError(ValueError):
    """Candle data cannot be used by the registered strategy."""


class SignalDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NOTHING = "nothing"


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal, int, or an exact string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        validate_decimal_bounds(result, field=field)
    except (DecimalException, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a bounded finite decimal") from error
    if positive and result <= 0:
        raise ValueError(f"{field} must be greater than zero")
    if nonnegative and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


@dataclass(frozen=True, slots=True)
class Candle:
    """One immutable exchange candle.

    ``received_at`` is retained separately from exchange time so callers can
    prove the completed observation was available before a signal expired.
    """

    instrument: str
    interval: str
    open_time: datetime
    close_time: datetime
    received_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    complete: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.instrument, str)
            or not self.instrument
            or self.instrument != self.instrument.strip()
        ):
            raise ValueError("instrument must be a non-empty, trimmed string")
        if self.interval != "4h":
            raise ValueError("candidate-v0 accepts only interval '4h'")
        open_time = _instant(self.open_time, "open_time")
        close_time = _instant(self.close_time, "close_time")
        received_at = _instant(self.received_at, "received_at")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)
        object.__setattr__(self, "received_at", received_at)
        if close_time - open_time != FOUR_HOURS:
            raise ValueError("a 4h candle must span exactly four hours")
        if (
            open_time.hour % 4 != 0
            or open_time.minute != 0
            or open_time.second != 0
            or open_time.microsecond != 0
        ):
            raise ValueError("4h candle open_time must align to a UTC four-hour boundary")
        if received_at < close_time:
            raise ValueError("received_at cannot precede candle close_time")
        if type(self.complete) is not bool:
            raise TypeError("complete must be bool")

        for field in ("open", "high", "low", "close"):
            object.__setattr__(
                self,
                field,
                _decimal(getattr(self, field), field, positive=True),
            )
        object.__setattr__(
            self, "volume", _decimal(self.volume, "volume", nonnegative=True)
        )
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("candle high/low do not contain open and close")
        if self.low > self.high:
            raise ValueError("candle low cannot exceed high")


@dataclass(frozen=True, slots=True)
class RegisteredStrategy:
    """The exact, preregistered candidate-v0 parameter set.

    The constructor rejects parameter drift under the same strategy identity.
    Researching another value requires a separate strategy type/version rather
    than mutating this registration after outcomes are known.
    """

    strategy_id: str = "candidate-v0"
    strategy_version: str = "1"
    interval: str = "4h"
    ema_fast: int = 50
    ema_slow: int = 200
    donchian_period: int = 20
    atr_period: int = 14
    warmup_bars: int = 1000
    signal_expiry_seconds: int = 900
    stop_atr_multiple: Decimal = Decimal("1.5")
    target_atr_multiple: Decimal = Decimal("3")
    max_holding_bars: int = 12
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.strategy_id != "candidate-v0" or self.strategy_version != "1":
            raise ValueError("this module implements only candidate-v0 version 1")
        frozen_integer_fields = {
            "ema_fast": 50,
            "ema_slow": 200,
            "donchian_period": 20,
            "atr_period": 14,
            "warmup_bars": 1000,
            "signal_expiry_seconds": 900,
            "max_holding_bars": 12,
            "schema_version": 1,
        }
        if self.interval != "4h":
            raise ValueError("candidate-v0 interval is frozen at 4h")
        for field, expected in frozen_integer_fields.items():
            value = getattr(self, field)
            if type(value) is not int or value != expected:
                raise ValueError(f"candidate-v0 {field} is frozen at {expected}")
        object.__setattr__(
            self,
            "stop_atr_multiple",
            _decimal(self.stop_atr_multiple, "stop_atr_multiple", positive=True),
        )
        object.__setattr__(
            self,
            "target_atr_multiple",
            _decimal(self.target_atr_multiple, "target_atr_multiple", positive=True),
        )
        if self.stop_atr_multiple != Decimal("1.5"):
            raise ValueError("candidate-v0 stop_atr_multiple is frozen at 1.5")
        if self.target_atr_multiple != Decimal("3"):
            raise ValueError("candidate-v0 target_atr_multiple is frozen at 3")

    @property
    def registration_hash(self) -> str:
        return domain_hash(STRATEGY_HASH_DOMAIN, self)


CANDIDATE_V0 = RegisteredStrategy()


@dataclass(frozen=True, slots=True)
class StrategyFeatures:
    bar_index: int
    ema_fast: Decimal
    ema_slow: Decimal
    atr: Decimal
    donchian_high: Decimal
    donchian_low: Decimal
    previous_donchian_high: Decimal
    previous_donchian_low: Decimal


@dataclass(frozen=True, slots=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    strategy_hash: str
    instrument: str
    bar_index: int
    signal_time: datetime
    observed_at: datetime
    expires_at: datetime
    direction: SignalDirection
    reason: str
    close: Decimal
    features: StrategyFeatures
    candle_chain_hash: str

    @property
    def signal_hash(self) -> str:
        return domain_hash(SIGNAL_HASH_DOMAIN, self)


def validate_candle_series(candles: Iterable[Candle]) -> tuple[Candle, ...]:
    """Return a validated immutable series or fail closed.

    Missing bars are not forward-filled.  Duplicate, out-of-order, partial,
    cross-instrument, or late-interval data is rejected before indicators run.
    """

    values = tuple(candles)
    if not values:
        raise StrategyDataError("candle series must not be empty")
    if any(not isinstance(candle, Candle) for candle in values):
        raise TypeError("candle series must contain only Candle values")
    instrument = values[0].instrument
    previous: Candle | None = None
    for index, candle in enumerate(values):
        if not candle.complete:
            raise StrategyDataError(f"candle[{index}] is not complete")
        if candle.instrument != instrument:
            raise StrategyDataError("candle series mixes instruments")
        if candle.interval != "4h":
            raise StrategyDataError("candle series mixes intervals")
        if previous is not None:
            if candle.open_time != previous.close_time:
                raise StrategyDataError(
                    "candle series must be strictly ordered, unique, and contiguous"
                )
            if candle.received_at < previous.received_at:
                raise StrategyDataError("candle receipt times must be nondecreasing")
        previous = candle
    return values


def _ema(values: Sequence[Decimal], period: int) -> tuple[Decimal | None, ...]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return tuple(result)
    with localcontext(_CALCULATION_CONTEXT) as context:
        seed = context.divide(sum(values[:period], Decimal("0")), Decimal(period))
        result[period - 1] = seed
        alpha = context.divide(Decimal("2"), Decimal(period + 1))
        one_minus_alpha = context.subtract(Decimal("1"), alpha)
        current = seed
        for index in range(period, len(values)):
            current = context.add(
                context.multiply(values[index], alpha),
                context.multiply(current, one_minus_alpha),
            )
            result[index] = current
    return tuple(result)


def _atr(candles: Sequence[Candle], period: int) -> tuple[Decimal | None, ...]:
    result: list[Decimal | None] = [None] * len(candles)
    true_ranges: list[Decimal] = []
    with localcontext(_CALCULATION_CONTEXT) as context:
        for index, candle in enumerate(candles):
            high_low = context.subtract(candle.high, candle.low)
            if index == 0:
                true_range = high_low
            else:
                previous_close = candles[index - 1].close
                true_range = max(
                    high_low,
                    abs(context.subtract(candle.high, previous_close)),
                    abs(context.subtract(candle.low, previous_close)),
                )
            true_ranges.append(true_range)
        if len(true_ranges) < period:
            return tuple(result)
        current = context.divide(
            sum(true_ranges[:period], Decimal("0")), Decimal(period)
        )
        result[period - 1] = current
        for index in range(period, len(true_ranges)):
            current = context.divide(
                context.add(
                    context.multiply(current, Decimal(period - 1)),
                    true_ranges[index],
                ),
                Decimal(period),
            )
            result[index] = current
    return tuple(result)


def _candle_chain(candles: Sequence[Candle]) -> tuple[str, ...]:
    chain: list[str] = []
    previous = "genesis"
    for candle in candles:
        previous = domain_hash(
            CANDLE_CHAIN_HASH_DOMAIN,
            {"previous": previous, "candle": candle},
        )
        chain.append(previous)
    return tuple(chain)


def feature_series(
    candles: Iterable[Candle],
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> tuple[StrategyFeatures | None, ...]:
    """Calculate candidate-v0 features without producing a direction."""

    if not isinstance(strategy, RegisteredStrategy):
        raise TypeError("strategy must be RegisteredStrategy")
    values = validate_candle_series(candles)
    closes = tuple(candle.close for candle in values)
    fast = _ema(closes, strategy.ema_fast)
    slow = _ema(closes, strategy.ema_slow)
    atr = _atr(values, strategy.atr_period)
    features: list[StrategyFeatures | None] = [None] * len(values)
    first_channel_index = strategy.donchian_period + 1
    for index in range(first_channel_index, len(values)):
        if fast[index] is None or slow[index] is None or atr[index] is None:
            continue
        current_window = values[index - strategy.donchian_period : index]
        previous_window = values[
            index - strategy.donchian_period - 1 : index - 1
        ]
        features[index] = StrategyFeatures(
            bar_index=index,
            ema_fast=fast[index],
            ema_slow=slow[index],
            atr=atr[index],
            donchian_high=max(candle.high for candle in current_window),
            donchian_low=min(candle.low for candle in current_window),
            previous_donchian_high=max(candle.high for candle in previous_window),
            previous_donchian_low=min(candle.low for candle in previous_window),
        )
    return tuple(features)


def scan_signals(
    candles: Iterable[Candle],
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> tuple[StrategySignal, ...]:
    """Classify every post-warm-up completed candle as buy/sell/nothing."""

    if not isinstance(strategy, RegisteredStrategy):
        raise TypeError("strategy must be RegisteredStrategy")
    values = validate_candle_series(candles)
    features = feature_series(values, strategy)
    chain = _candle_chain(values)
    if len(values) <= strategy.warmup_bars:
        return ()

    signals: list[StrategySignal] = []
    for index in range(strategy.warmup_bars, len(values)):
        current = values[index]
        calculated = features[index]
        if calculated is None:  # Defensive; the 1000-bar warm-up is ample.
            raise StrategyDataError("indicator warm-up did not produce features")
        previous_close = values[index - 1].close
        long_transition = (
            current.close > calculated.donchian_high
            and previous_close <= calculated.previous_donchian_high
        )
        short_transition = (
            current.close < calculated.donchian_low
            and previous_close >= calculated.previous_donchian_low
        )
        long_trend = (
            calculated.ema_fast > calculated.ema_slow
            and calculated.ema_fast > features[index - 1].ema_fast  # type: ignore[union-attr]
        )
        short_trend = (
            calculated.ema_fast < calculated.ema_slow
            and calculated.ema_fast < features[index - 1].ema_fast  # type: ignore[union-attr]
        )

        if long_transition and long_trend:
            direction = SignalDirection.BUY
            reason = "donchian_up_transition_with_rising_bull_trend"
        elif short_transition and short_trend:
            direction = SignalDirection.SELL
            reason = "donchian_down_transition_with_falling_bear_trend"
        elif long_transition:
            direction = SignalDirection.NOTHING
            reason = "up_transition_failed_trend_filter"
        elif short_transition:
            direction = SignalDirection.NOTHING
            reason = "down_transition_failed_trend_filter"
        else:
            direction = SignalDirection.NOTHING
            reason = "no_donchian_transition"

        signals.append(
            StrategySignal(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.strategy_version,
                strategy_hash=strategy.registration_hash,
                instrument=current.instrument,
                bar_index=index,
                signal_time=current.close_time,
                observed_at=current.received_at,
                expires_at=current.close_time
                + timedelta(seconds=strategy.signal_expiry_seconds),
                direction=direction,
                reason=reason,
                close=current.close,
                features=calculated,
                candle_chain_hash=chain[index],
            )
        )
    return tuple(signals)


def latest_signal(
    candles: Iterable[Candle],
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> StrategySignal | None:
    """Return the latest classification, or ``None`` before full warm-up."""

    signals = scan_signals(candles, strategy)
    return signals[-1] if signals else None


__all__ = (
    "CANDIDATE_V0",
    "Candle",
    "RegisteredStrategy",
    "SignalDirection",
    "StrategyDataError",
    "StrategyFeatures",
    "StrategySignal",
    "feature_series",
    "latest_signal",
    "scan_signals",
    "validate_candle_series",
)
