from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import unittest

from trading_harness.analysis import (
    Candle,
    TechnicalBias,
    TechnicalConfig,
    analyze_technical,
)
from trading_harness.errors import ValidationError


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles_from_closes(
    closes: list[str],
    *,
    symbol: str = "ETH",
    interval: str = "4h",
) -> list[Candle]:
    result: list[Candle] = []
    previous = Decimal(closes[0])
    for index, raw_close in enumerate(closes):
        close = Decimal(raw_close)
        opened = START + timedelta(hours=4 * index)
        result.append(
            Candle(
                symbol=symbol,
                interval=interval,
                open_time=opened,
                close_time=opened + timedelta(hours=4),
                open=previous,
                high=max(previous, close) + Decimal("0.5"),
                low=min(previous, close) - Decimal("0.5"),
                close=close,
                volume=Decimal("100") + index,
            )
        )
        previous = close
    return result


def short_config(**changes: object) -> TechnicalConfig:
    values: dict[str, object] = {
        "version": "test-profile-v1",
        "fast_period": 2,
        "slow_period": 3,
        "trend_period": 4,
        "rsi_period": 2,
        "atr_period": 2,
        "rsi_buy_min": Decimal("1"),
        "rsi_buy_max": Decimal("100"),
        "rsi_sell_min": Decimal("0"),
        "rsi_sell_max": Decimal("0"),
        "stop_atr_multiple": Decimal("2"),
        "reward_risk_multiple": Decimal("2"),
    }
    values.update(changes)
    return TechnicalConfig(**values)  # type: ignore[arg-type]


class CandleContractTests(unittest.TestCase):
    def test_rejects_invalid_ohlc_or_time(self) -> None:
        with self.assertRaisesRegex(ValidationError, "high"):
            Candle(
                symbol="ETH",
                interval="4h",
                open_time=START,
                close_time=START + timedelta(hours=4),
                open=Decimal("10"),
                high=Decimal("9"),
                low=Decimal("8"),
                close=Decimal("10"),
                volume=Decimal("1"),
            )
        with self.assertRaisesRegex(ValidationError, "close_time"):
            Candle(
                symbol="ETH",
                interval="4h",
                open_time=START,
                close_time=START,
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10"),
                volume=Decimal("1"),
            )


class TechnicalAnalysisTests(unittest.TestCase):
    def test_buy_snapshot_has_mandatory_research_stop_and_two_r_target(self) -> None:
        series = candles_from_closes(["10", "11", "12", "13", "14", "15"])
        as_of = series[-1].close_time

        snapshot = analyze_technical(series, as_of=as_of, config=short_config())

        self.assertIs(snapshot.bias, TechnicalBias.BUY)
        self.assertIsNotNone(snapshot.stop_price)
        self.assertIsNotNone(snapshot.target_price)
        stop_distance = snapshot.close - snapshot.stop_price
        reward_distance = snapshot.target_price - snapshot.close
        self.assertEqual(reward_distance, stop_distance * 2)
        self.assertFalse(snapshot.executable)
        self.assertEqual(snapshot.as_dict()["evidence_status"], "research_candidate")

    def test_sell_snapshot_is_symmetric(self) -> None:
        series = candles_from_closes(["15", "14", "13", "12", "11", "10"])
        config = short_config(
            rsi_buy_min=Decimal("100"),
            rsi_buy_max=Decimal("100"),
            rsi_sell_min=Decimal("0"),
            rsi_sell_max=Decimal("99"),
        )

        snapshot = analyze_technical(
            series,
            as_of=series[-1].close_time,
            config=config,
        )

        self.assertIs(snapshot.bias, TechnicalBias.SELL)
        self.assertGreater(snapshot.stop_price, snapshot.close)
        self.assertLess(snapshot.target_price, snapshot.close)
        self.assertEqual(
            snapshot.close - snapshot.target_price,
            (snapshot.stop_price - snapshot.close) * 2,
        )

    def test_incomplete_final_candle_is_ignored_and_hashed_out(self) -> None:
        complete = candles_from_closes(["10", "11", "12", "13", "14", "15"])
        future = candles_from_closes(["16"])[0]
        future = Candle(
            symbol="ETH",
            interval="4h",
            open_time=complete[-1].close_time,
            close_time=complete[-1].close_time + timedelta(hours=4),
            open=complete[-1].close,
            high=Decimal("16.5"),
            low=Decimal("14.5"),
            close=Decimal("16"),
            volume=Decimal("100"),
        )

        baseline = analyze_technical(
            complete,
            as_of=complete[-1].close_time,
            config=short_config(),
        )
        with_future = analyze_technical(
            [*complete, future],
            as_of=complete[-1].close_time,
            config=short_config(),
        )

        self.assertEqual(with_future.ignored_incomplete_candles, 1)
        self.assertEqual(with_future.data_hash, baseline.data_hash)
        self.assertEqual(with_future.close, baseline.close)

    def test_results_ignore_ambient_decimal_precision(self) -> None:
        series = candles_from_closes(["10", "11", "10", "12", "11", "13"])
        config = short_config(
            rsi_buy_min=Decimal("1"),
            rsi_buy_max=Decimal("100"),
        )
        with localcontext() as context:
            context.prec = 6
            low_precision = analyze_technical(
                series,
                as_of=series[-1].close_time,
                config=config,
            ).as_dict()
        with localcontext() as context:
            context.prec = 50
            high_precision = analyze_technical(
                series,
                as_of=series[-1].close_time,
                config=config,
            ).as_dict()

        self.assertEqual(low_precision, high_precision)

    def test_insufficient_mixed_or_unordered_inputs_fail_closed(self) -> None:
        series = candles_from_closes(["10", "11", "12"])
        with self.assertRaisesRegex(ValidationError, "at least"):
            analyze_technical(
                series,
                as_of=series[-1].close_time,
                config=short_config(),
            )

        enough = candles_from_closes(["10", "11", "12", "13", "14", "15"])
        mixed = [*enough[:-1], candles_from_closes(["15"], symbol="BTC")[0]]
        mixed[-1] = Candle(
            symbol="BTC",
            interval="4h",
            open_time=enough[-1].open_time,
            close_time=enough[-1].close_time,
            open=enough[-1].open,
            high=enough[-1].high,
            low=enough[-1].low,
            close=enough[-1].close,
            volume=enough[-1].volume,
        )
        with self.assertRaisesRegex(ValidationError, "same symbol"):
            analyze_technical(mixed, as_of=enough[-1].close_time, config=short_config())
        with self.assertRaisesRegex(ValidationError, "strictly ordered"):
            analyze_technical(
                [*enough[:-2], enough[-1], enough[-2]],
                as_of=enough[-1].close_time,
                config=short_config(),
            )


if __name__ == "__main__":
    unittest.main()
