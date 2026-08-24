from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import json
import unittest

from trading_harness.errors import ValidationError
from trading_harness.history import (
    CANDLE_HISTORY_HASH_DOMAIN,
    HYPERLIQUID_CANDLE_LIMIT,
    CandleHistoryConflictError,
    CandleHistoryGapError,
    CandleHistoryResponseError,
    CandleHistoryTransportError,
    fetch_candle_history,
    interval_duration_ms,
    supported_intervals,
)


MAINNET_INFO = "https://api.hyperliquid.xyz/info"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
MINUTE_MS = 60_000
BASE_MS = 1_710_000_000_000
AS_OF_MS = BASE_MS + 6 * MINUTE_MS
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def datetime_from_ms(value: int) -> datetime:
    return EPOCH + timedelta(milliseconds=value)


def fixed_clock(value: int = AS_OF_MS):
    def read() -> datetime:
        return datetime_from_ms(value)

    return read


def raw_candle(
    open_time_ms: int,
    *,
    symbol: object = "ETH",
    interval: object = "1m",
    opened: object = "100.00",
    high: object = "102.0",
    low: object = "99.000",
    closed: object = "101.00",
    volume: object = "12.5000",
    trades: object = 7,
) -> dict[str, object]:
    duration = (
        interval_duration_ms(interval)
        if isinstance(interval, str) and interval in supported_intervals()
        else MINUTE_MS
    )
    return {
        "t": open_time_ms,
        "T": open_time_ms + duration - 1,
        "s": symbol,
        "i": interval,
        "o": opened,
        "h": high,
        "l": low,
        "c": closed,
        "v": volume,
        "n": trades,
    }


class FixtureTransport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload: object) -> object:
        self.calls.append((endpoint, deepcopy(dict(payload))))  # type: ignore[arg-type]
        return deepcopy(self.response)


def fetch(
    response: object,
    *,
    start: int = BASE_MS,
    end: int = BASE_MS + 2 * MINUTE_MS,
    as_of: int = AS_OF_MS,
    network: str = "mainnet",
):
    transport = FixtureTransport(response)
    result = fetch_candle_history(
        "ETH",
        "1m",
        start,
        end,
        network,
        transport=transport,
        clock=fixed_clock(as_of),
    )
    return result, transport


class IntervalContractTests(unittest.TestCase):
    def test_official_interval_allowlist_and_fixed_durations(self) -> None:
        self.assertEqual(
            supported_intervals(),
            (
                "1m",
                "3m",
                "5m",
                "15m",
                "30m",
                "1h",
                "2h",
                "4h",
                "8h",
                "12h",
                "1d",
                "3d",
                "1w",
                "1M",
            ),
        )
        self.assertEqual(interval_duration_ms("1h"), 3_600_000)
        self.assertEqual(interval_duration_ms("1M"), 30 * 86_400_000)
        for invalid in ("60m", "1mo", "1M ", "", 60):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    interval_duration_ms(invalid)  # type: ignore[arg-type]


class SuccessfulIngestionTests(unittest.TestCase):
    def test_fetches_allowlisted_endpoint_and_builds_exact_history(self) -> None:
        response = [raw_candle(BASE_MS + index * MINUTE_MS) for index in range(3)]

        # Ambient Decimal precision and traps must not change parsing or hash.
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            history, transport = fetch(
                response,
                start=BASE_MS + 123,
                end=BASE_MS + 2 * MINUTE_MS + 999,
                network="testnet",
            )

        self.assertEqual(
            transport.calls,
            [
                (
                    TESTNET_INFO,
                    {
                        "type": "candleSnapshot",
                        "req": {
                            "coin": "ETH",
                            "interval": "1m",
                            "startTime": BASE_MS,
                            "endTime": BASE_MS + 2 * MINUTE_MS,
                        },
                    },
                )
            ],
        )
        self.assertEqual(history.network, "testnet")
        self.assertEqual(history.source_url, TESTNET_INFO)
        self.assertTrue(history.coverage_complete)
        self.assertFalse(history.truncated)
        self.assertEqual(history.expected_completed_candles, 3)
        self.assertEqual(len(history.candles), 3)
        self.assertEqual(history.candles[0].open, Decimal("100.00"))
        self.assertEqual(history.candles[0].volume, Decimal("12.5000"))
        self.assertRegex(history.data_hash, r"^[0-9a-f]{64}$")

        document = history.as_dict()
        self.assertEqual(document["schema_version"], "hyperliquid.candle_history.v1")
        self.assertEqual(document["data_hash_domain"], CANDLE_HISTORY_HASH_DOMAIN)
        self.assertEqual(
            document["request"],
            {
                "start_time_ms": BASE_MS + 123,
                "end_time_ms": BASE_MS + 2 * MINUTE_MS + 999,
                "query_start_open_time_ms": BASE_MS,
                "query_end_open_time_ms": BASE_MS + 2 * MINUTE_MS,
                "boundaries": "inclusive_candle_open_times",
            },
        )
        self.assertEqual(document["candles"][0]["o"], "100")  # type: ignore[index]
        self.assertEqual(document["candles"][0]["v"], "12.5")  # type: ignore[index]
        json.dumps(document, allow_nan=False, sort_keys=True)

    def test_drops_only_the_current_trailing_incomplete_candle(self) -> None:
        as_of = BASE_MS + 2 * MINUTE_MS + 30_000
        response = [raw_candle(BASE_MS + index * MINUTE_MS) for index in range(3)]

        history, _ = fetch(response, end=BASE_MS + 2 * MINUTE_MS, as_of=as_of)

        self.assertEqual(len(history.candles), 2)
        self.assertEqual(history.dropped_incomplete_count, 1)
        self.assertEqual(history.expected_completed_candles, 2)
        self.assertTrue(history.coverage_complete)
        self.assertEqual(
            history.as_dict()["coverage"]["last_open_time_ms"],  # type: ignore[index]
            BASE_MS + MINUTE_MS,
        )

        completed_only, _ = fetch(
            response[:2],
            end=BASE_MS + MINUTE_MS,
            as_of=as_of,
        )
        self.assertEqual(history.data_hash, completed_only.data_hash)

    def test_exact_duplicates_are_deduplicated_and_hash_is_order_independent(self) -> None:
        first = raw_candle(BASE_MS)
        equivalent_first = raw_candle(
            BASE_MS,
            opened="1E+2",
            high="102.00",
            low="99",
            closed="101.0",
            volume="12.5",
        )
        second = raw_candle(BASE_MS + MINUTE_MS)

        baseline, _ = fetch([first, second], end=BASE_MS + MINUTE_MS)
        merged, _ = fetch(
            [second, equivalent_first, first],
            end=BASE_MS + MINUTE_MS,
        )

        self.assertEqual(merged.response_count, 3)
        self.assertEqual(merged.unique_response_count, 2)
        self.assertEqual(merged.duplicate_count, 1)
        self.assertEqual(merged.data_hash, baseline.data_hash)
        self.assertEqual(merged.candles, baseline.candles)

    def test_converts_to_analysis_candles_without_float_or_time_loss(self) -> None:
        history, _ = fetch([raw_candle(BASE_MS)], end=BASE_MS)

        converted = history.technical_candles()

        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0].open, Decimal("100"))
        self.assertEqual(converted[0].open_time, datetime_from_ms(BASE_MS))
        self.assertEqual(
            converted[0].close_time,
            datetime_from_ms(BASE_MS + MINUTE_MS - 1),
        )

    def test_current_only_range_can_validly_produce_no_completed_candles(self) -> None:
        as_of = BASE_MS + 30_000
        history, _ = fetch(
            [raw_candle(BASE_MS)],
            start=BASE_MS,
            end=BASE_MS,
            as_of=as_of,
        )

        self.assertEqual(history.candles, ())
        self.assertEqual(history.expected_completed_candles, 0)
        self.assertEqual(history.dropped_incomplete_count, 1)
        self.assertTrue(history.coverage_complete)


class CoverageAndConflictTests(unittest.TestCase):
    def test_rejects_conflicting_duplicate_open_times(self) -> None:
        duplicate = raw_candle(BASE_MS, closed="100.5")

        with self.assertRaises(CandleHistoryConflictError):
            fetch([raw_candle(BASE_MS), duplicate], end=BASE_MS)

    def test_rejects_internal_and_unexplained_edge_gaps(self) -> None:
        internal_gap = [raw_candle(BASE_MS), raw_candle(BASE_MS + 2 * MINUTE_MS)]
        missing_start = [
            raw_candle(BASE_MS + MINUTE_MS),
            raw_candle(BASE_MS + 2 * MINUTE_MS),
        ]
        missing_end = [raw_candle(BASE_MS), raw_candle(BASE_MS + MINUTE_MS)]

        for response in (internal_gap, missing_start, missing_end):
            with self.subTest(response=response):
                with self.assertRaises(CandleHistoryGapError):
                    fetch(response)

    def test_surfaces_a_contiguous_5000_row_tail_as_truncated(self) -> None:
        expected_count = HYPERLIQUID_CANDLE_LIMIT + 1
        end = BASE_MS + (expected_count - 1) * MINUTE_MS
        response = [
            raw_candle(BASE_MS + index * MINUTE_MS)
            for index in range(1, expected_count)
        ]

        history, _ = fetch(
            response,
            start=BASE_MS,
            end=end,
            as_of=end + MINUTE_MS,
        )

        self.assertEqual(history.response_count, HYPERLIQUID_CANDLE_LIMIT)
        self.assertTrue(history.at_response_limit)
        self.assertTrue(history.truncated)
        self.assertFalse(history.coverage_complete)
        self.assertEqual(history.truncation_reason, "hyperliquid_5000_candle_limit")
        self.assertEqual(history.expected_completed_candles, expected_count)
        self.assertEqual(history.candles[0].open_time_ms, BASE_MS + MINUTE_MS)
        coverage = history.as_dict()["coverage"]
        self.assertEqual(coverage["completed_candles"], HYPERLIQUID_CANDLE_LIMIT)  # type: ignore[index]
        self.assertTrue(coverage["truncated"])  # type: ignore[index]

    def test_exactly_5000_requested_rows_can_be_complete_not_truncated(self) -> None:
        response = [
            raw_candle(BASE_MS + index * MINUTE_MS)
            for index in range(HYPERLIQUID_CANDLE_LIMIT)
        ]
        end = response[-1]["t"]

        history, _ = fetch(
            response,
            end=end,  # type: ignore[arg-type]
            as_of=end + MINUTE_MS,  # type: ignore[operator]
        )

        self.assertTrue(history.at_response_limit)
        self.assertTrue(history.coverage_complete)
        self.assertFalse(history.truncated)

    def test_duplicate_padding_cannot_masquerade_as_limit_truncation(self) -> None:
        second = raw_candle(BASE_MS + MINUTE_MS)
        third = raw_candle(BASE_MS + 2 * MINUTE_MS)
        response = [second] * (HYPERLIQUID_CANDLE_LIMIT - 1) + [third]

        with self.assertRaises(CandleHistoryGapError):
            fetch(response)


class StrictSchemaTests(unittest.TestCase):
    def assert_response_rejected(self, response: object) -> None:
        with self.assertRaises(CandleHistoryResponseError):
            fetch(response, end=BASE_MS)

    def test_requires_array_and_exact_object_fields(self) -> None:
        self.assert_response_rejected({"candles": []})

        missing = raw_candle(BASE_MS)
        del missing["n"]
        self.assert_response_rejected([missing])

        extra = raw_candle(BASE_MS)
        extra["unexpected"] = "field"
        self.assert_response_rejected([extra])

    def test_rejects_non_exact_or_invalid_numbers(self) -> None:
        mutations = {
            "o": 100.0,
            "h": "NaN",
            "l": "0",
            "c": "Infinity",
            "v": "-1",
            "n": True,
        }
        for field, value in mutations.items():
            with self.subTest(field=field, value=value):
                candle = raw_candle(BASE_MS)
                candle[field] = value
                self.assert_response_rejected([candle])

        bad_high = raw_candle(BASE_MS, high="100")
        bad_low = raw_candle(BASE_MS, low="101.5")
        self.assert_response_rejected([bad_high])
        self.assert_response_rejected([bad_low])

    def test_rejects_bad_time_grid_symbol_interval_and_future_open(self) -> None:
        misaligned = raw_candle(BASE_MS)
        misaligned["t"] = BASE_MS + 1
        misaligned["T"] = BASE_MS + MINUTE_MS

        bad_close = raw_candle(BASE_MS)
        bad_close["T"] = BASE_MS + MINUTE_MS

        wrong_symbol = raw_candle(BASE_MS, symbol="BTC")
        wrong_interval = raw_candle(BASE_MS, interval="5m")

        for response in ([misaligned], [bad_close], [wrong_symbol], [wrong_interval]):
            with self.subTest(response=response):
                self.assert_response_rejected(response)

        future = raw_candle(BASE_MS + 2 * MINUTE_MS)
        with self.assertRaises(CandleHistoryResponseError):
            fetch(
                [future],
                start=BASE_MS,
                end=BASE_MS,
                as_of=BASE_MS + MINUTE_MS,
            )

    def test_rejects_more_than_the_documented_response_limit_before_parsing(self) -> None:
        with self.assertRaisesRegex(CandleHistoryResponseError, "exceeds 5000"):
            fetch([{}] * (HYPERLIQUID_CANDLE_LIMIT + 1), end=BASE_MS)


class ArgumentAndTransportTests(unittest.TestCase):
    def test_invalid_arguments_fail_before_transport(self) -> None:
        transport = FixtureTransport([])
        cases = (
            (" ETH", "1m", BASE_MS, BASE_MS, "mainnet"),
            ("ETH/USDC", "1m", BASE_MS, BASE_MS, "mainnet"),
            ("ETH", "60m", BASE_MS, BASE_MS, "mainnet"),
            ("ETH", "1m", BASE_MS + 1, BASE_MS, "mainnet"),
            ("ETH", "1m", BASE_MS, AS_OF_MS + 1, "mainnet"),
            ("ETH", "1m", BASE_MS, BASE_MS, "MAINNET"),
        )
        for symbol, interval, start, end, network in cases:
            with self.subTest(
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                network=network,
            ):
                with self.assertRaises(ValidationError):
                    fetch_candle_history(
                        symbol,
                        interval,
                        start,
                        end,
                        network,
                        transport=transport,
                        clock=fixed_clock(),
                    )
        self.assertEqual(transport.calls, [])

    def test_transport_failures_are_sanitized(self) -> None:
        def broken(endpoint: str, payload: Mapping[str, object]) -> object:
            del endpoint, payload
            raise RuntimeError("private response body")

        with self.assertRaises(CandleHistoryTransportError) as caught:
            fetch_candle_history(
                "ETH",
                "1m",
                BASE_MS,
                BASE_MS,
                "mainnet",
                transport=broken,
                clock=fixed_clock(),
            )
        self.assertNotIn("private response body", str(caught.exception))

    def test_mainnet_endpoint_is_explicit(self) -> None:
        history, transport = fetch([raw_candle(BASE_MS)], end=BASE_MS)

        self.assertEqual(history.source_url, MAINNET_INFO)
        self.assertEqual(transport.calls[0][0], MAINNET_INFO)


if __name__ == "__main__":
    unittest.main()
