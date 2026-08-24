from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
import unittest

from trading_harness.strategy import (
    CANDIDATE_V0,
    Candle,
    RegisteredStrategy,
    SignalDirection,
    StrategyDataError,
    feature_series,
    latest_signal,
    scan_signals,
)


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def candle(
    index: int,
    close: Decimal,
    *,
    high_offset: Decimal = Decimal("0.5"),
    low_offset: Decimal = Decimal("0.5"),
    complete: bool = True,
    received_delay: timedelta = timedelta(0),
) -> Candle:
    opened = START + timedelta(hours=4 * index)
    return Candle(
        instrument="ETH",
        interval="4h",
        open_time=opened,
        close_time=opened + timedelta(hours=4),
        received_at=opened + timedelta(hours=4) + received_delay,
        open=close,
        high=close + high_offset,
        low=close - low_offset,
        close=close,
        volume=Decimal("1000"),
        complete=complete,
    )


def rising_breakout_series(extra: int = 1) -> tuple[Candle, ...]:
    values = [Decimal("100") + Decimal(index) / Decimal("100") for index in range(1000)]
    values.append(Decimal("112"))
    for index in range(extra - 1):
        values.append(Decimal("113") + Decimal(index))
    return tuple(candle(index, value) for index, value in enumerate(values))


def falling_breakout_series() -> tuple[Candle, ...]:
    values = [Decimal("200") - Decimal(index) / Decimal("100") for index in range(1000)]
    values.append(Decimal("188"))
    return tuple(candle(index, value) for index, value in enumerate(values))


class CandleTests(unittest.TestCase):
    def test_rejects_binary_float_prices(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be Decimal"):
            Candle(
                instrument="ETH",
                interval="4h",
                open_time=START,
                close_time=START + timedelta(hours=4),
                received_at=START + timedelta(hours=4),
                open=100.0,  # type: ignore[arg-type]
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )

    def test_requires_exact_utc_four_hour_alignment(self) -> None:
        with self.assertRaisesRegex(ValueError, "align"):
            Candle(
                instrument="ETH",
                interval="4h",
                open_time=START + timedelta(hours=1),
                close_time=START + timedelta(hours=5),
                received_at=START + timedelta(hours=5),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )


class RegisteredStrategyTests(unittest.TestCase):
    def test_registration_is_frozen(self) -> None:
        with self.assertRaisesRegex(ValueError, "warmup_bars"):
            RegisteredStrategy(warmup_bars=999)
        with self.assertRaisesRegex(ValueError, "stop_atr_multiple"):
            RegisteredStrategy(stop_atr_multiple=Decimal("1"))
        self.assertEqual(len(CANDIDATE_V0.registration_hash), 64)

    def test_returns_no_classification_before_full_warmup(self) -> None:
        values = tuple(candle(index, Decimal("100")) for index in range(1000))
        self.assertEqual(scan_signals(values), ())
        self.assertIsNone(latest_signal(values))

    def test_detects_one_long_transition_and_does_not_repeat_state(self) -> None:
        signals = scan_signals(rising_breakout_series(extra=2))
        self.assertEqual(signals[0].bar_index, 1000)
        self.assertIs(signals[0].direction, SignalDirection.BUY)
        self.assertEqual(
            signals[0].reason,
            "donchian_up_transition_with_rising_bull_trend",
        )
        self.assertEqual(signals[0].expires_at - signals[0].signal_time, timedelta(minutes=15))
        self.assertIs(signals[1].direction, SignalDirection.NOTHING)
        self.assertEqual(signals[1].reason, "no_donchian_transition")

    def test_detects_short_transition(self) -> None:
        signal = latest_signal(falling_breakout_series())
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIs(signal.direction, SignalDirection.SELL)
        self.assertEqual(
            signal.reason,
            "donchian_down_transition_with_falling_bear_trend",
        )

    def test_donchian_boundary_excludes_signal_bar_and_equality_abstains(self) -> None:
        values = list(rising_breakout_series())
        prior_high = max(item.high for item in values[980:1000])
        values[1000] = candle(1000, prior_high)
        signal = latest_signal(values)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.features.donchian_high, prior_high)
        self.assertIs(signal.direction, SignalDirection.NOTHING)

    def test_rejects_partial_and_gapped_series(self) -> None:
        partial = list(rising_breakout_series())
        partial[-1] = candle(1000, Decimal("112"), complete=False)
        with self.assertRaisesRegex(StrategyDataError, "not complete"):
            scan_signals(partial)

        gapped = list(rising_breakout_series())
        del gapped[500]
        with self.assertRaisesRegex(StrategyDataError, "contiguous"):
            feature_series(gapped)

    def test_indicator_and_signal_hash_ignore_ambient_decimal_context(self) -> None:
        values = rising_breakout_series()
        original_precision = getcontext().prec
        try:
            getcontext().prec = 8
            first = latest_signal(values)
            getcontext().prec = 50
            second = latest_signal(values)
        finally:
            getcontext().prec = original_precision
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.features, second.features)
        self.assertEqual(first.signal_hash, second.signal_hash)


if __name__ == "__main__":
    unittest.main()
