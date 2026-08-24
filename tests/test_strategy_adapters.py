from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trading_harness.errors import ValidationError
from trading_harness.history import fetch_candle_history
from trading_harness.strategy_adapters import backtest_candles, live_scan_candles


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
FOUR_HOURS_MS = 14_400_000


def raw(open_time: int, *, interval: str = "4h") -> dict[str, object]:
    duration = FOUR_HOURS_MS if interval == "4h" else 3_600_000
    return {
        "t": open_time,
        "T": open_time + duration - 1,
        "s": "ETH",
        "i": interval,
        "o": "100",
        "h": "102",
        "l": "99",
        "c": "101",
        "v": "12.5",
        "n": 7,
    }


def history(*, interval: str = "4h"):
    duration = FOUR_HOURS_MS if interval == "4h" else 3_600_000
    response = [raw(0, interval=interval), raw(duration, interval=interval)]
    return fetch_candle_history(
        "ETH",
        interval,
        0,
        duration,
        "testnet",
        transport=lambda _endpoint, _payload: response,
        clock=lambda: EPOCH + timedelta(milliseconds=duration * 3),
    )


class StrategyAdapterTests(unittest.TestCase):
    def test_inclusive_venue_close_becomes_exact_exclusive_boundary(self) -> None:
        converted = backtest_candles(history())

        self.assertEqual(converted[0].open_time, EPOCH)
        self.assertEqual(converted[0].close_time, EPOCH + timedelta(hours=4))
        self.assertEqual(converted[1].open_time, converted[0].close_time)
        self.assertEqual(converted[0].received_at, converted[0].close_time)
        self.assertEqual(converted[0].close, Decimal("101"))

    def test_live_scan_uses_actual_artifact_receipt_time(self) -> None:
        source = history()
        converted = live_scan_candles(source)
        expected = EPOCH + timedelta(milliseconds=source.as_of_time_ms)

        self.assertEqual({candle.received_at for candle in converted}, {expected})
        self.assertGreater(converted[-1].received_at, converted[-1].close_time)

    def test_truncated_or_wrong_interval_artifacts_fail_closed(self) -> None:
        source = history()
        broken = replace(
            source,
            coverage_complete=False,
            truncated=True,
            truncation_reason="test",
        )
        with self.assertRaisesRegex(ValidationError, "complete"):
            backtest_candles(broken)
        with self.assertRaisesRegex(ValidationError, "4h"):
            live_scan_candles(history(interval="1h"))


if __name__ == "__main__":
    unittest.main()
