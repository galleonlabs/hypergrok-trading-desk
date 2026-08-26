from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, Inexact, localcontext
import json
import unittest
from unittest import mock

from trading_harness.errors import ValidationError
from trading_harness import market_data
from trading_harness.market_data import (
    MarketDataResponseError,
    MarketDataTransportError,
    get_market_brief,
)


MAINNET_INFO = "https://api.hyperliquid.xyz/info"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
_UNSET = object()
RECEIVED_AT = datetime(2024, 3, 9, 16, 0, 0, 500_000, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return RECEIVED_AT


def fixture_brief(
    symbol: str,
    network: str,
    transport: object,
) -> dict[str, object]:
    return get_market_brief(  # type: ignore[arg-type]
        symbol,
        network,
        transport,
        clock=fixed_clock,
    )


def valid_context_response() -> list[object]:
    return [
        {
            "universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
            ]
        },
        [
            {
                "midPx": "60000",
                "markPx": "60001",
                "oraclePx": "59999",
                "funding": "-0.000001",
                "openInterest": "100",
                "dayNtlVlm": "900000000",
            },
            {
                "midPx": "3000.5000",
                "markPx": "3001.00",
                "oraclePx": "2999.500",
                "funding": "0.000012500",
                "openInterest": "412300.0",
                "dayNtlVlm": "1920000000.00",
            },
        ],
    ]


def valid_book_response() -> dict[str, object]:
    return {
        "coin": "ETH",
        "time": 1_710_000_000_123,
        "levels": [
            [
                {"px": "3000", "sz": "10.0", "n": 1},
                {"px": "2999", "sz": "20", "n": 2},
                {"px": "2995", "sz": "30", "n": 3},
                {"px": "2990", "sz": "40", "n": 4},
            ],
            [
                {"px": "3002", "sz": "11", "n": 1},
                {"px": "3003", "sz": "21", "n": 2},
                {"px": "3008", "sz": "31", "n": 3},
                {"px": "3010", "sz": "41", "n": 4},
            ],
        ],
    }


class FixtureTransport:
    def __init__(
        self,
        context: object = _UNSET,
        book: object = _UNSET,
    ) -> None:
        self.context = valid_context_response() if context is _UNSET else context
        self.book = valid_book_response() if book is _UNSET else book
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload: object) -> object:
        request = dict(payload)  # type: ignore[arg-type]
        self.calls.append((endpoint, request))
        if request == {"type": "metaAndAssetCtxs"}:
            return deepcopy(self.context)
        if request == {"type": "l2Book", "coin": "ETH"}:
            return deepcopy(self.book)
        raise AssertionError(f"unexpected public info request: {request!r}")


class MarketBriefTests(unittest.TestCase):
    def test_builds_exact_json_safe_market_brief_and_depth(self) -> None:
        transport = FixtureTransport()

        # The result must not depend on an embedding process's Decimal context.
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            brief = fixture_brief("eth", "mainnet", transport)

        self.assertEqual(
            transport.calls,
            [
                (MAINNET_INFO, {"type": "metaAndAssetCtxs"}),
                (MAINNET_INFO, {"type": "l2Book", "coin": "ETH"}),
            ],
        )
        self.assertEqual(brief["schema_version"], "hyperliquid.market_brief.v1")
        self.assertEqual(brief["venue"], "hyperliquid")
        self.assertEqual(brief["network"], "mainnet")
        self.assertEqual(brief["symbol"], "ETH")
        self.assertEqual(brief["observed_at"], "2024-03-09T16:00:00.123Z")
        self.assertEqual(brief["received_at"], "2024-03-09T16:00:00.500Z")
        self.assertEqual(brief["age_ms"], 377)
        self.assertEqual(
            brief["timestamps"],
            {
                "market_context": {
                    "exchange_observed_at": None,
                    "received_at": "2024-03-09T16:00:00.500Z",
                    "basis": "local_receipt_only",
                    "reason": "metaAndAssetCtxs has no exchange timestamp",
                },
                "book": {
                    "observed_at": "2024-03-09T16:00:00.123Z",
                    "received_at": "2024-03-09T16:00:00.500Z",
                    "age_ms": 377,
                    "basis": "hyperliquid_l2Book.time",
                },
                "top_level_observed_at_scope": "book_only",
            },
        )
        self.assertEqual(brief["mid"], "3000.5")
        self.assertEqual(brief["mark"], "3001")
        self.assertEqual(brief["oracle"], "2999.5")
        self.assertEqual(brief["funding_hourly"], "0.0000125")
        self.assertEqual(brief["open_interest"], "412300")
        self.assertEqual(brief["day_notional_volume"], "1920000000")
        self.assertEqual(
            brief["mid_consistency"],
            {
                "context_mid": "3000.5",
                "book_mid": "3001",
                "absolute_difference": "0.5",
                "divergence_bps": "1.666111296234588470509830056647784",
                "divergence_bps_exact": {
                    "numerator": "0.5",
                    "denominator": "3001",
                    "multiplier": "10000",
                },
                "divergence_bps_display_precision_digits": 34,
                "max_divergence_bps": "25",
                "comparison": (
                    "difference*10000 <= book_mid*max_divergence_bps"
                ),
                "within_limit": True,
            },
        )
        self.assertEqual(
            brief["sources"],
            [
                {
                    "url": MAINNET_INFO,
                    "endpoint": "/info",
                    "request_type": "metaAndAssetCtxs",
                },
                {
                    "url": MAINNET_INFO,
                    "endpoint": "/info",
                    "request_type": "l2Book",
                },
            ],
        )
        self.assertEqual(
            brief["book"],
            {
                "time_ms": 1_710_000_000_123,
                "mid": "3001",
                "best_bid": "3000",
                "best_ask": "3002",
                "bid_level_count": 4,
                "ask_level_count": 4,
                "level_cap_per_side": 20,
                "depth": {
                    "5bps": {
                        "bid_size": "10",
                        "ask_size": "11",
                        "bid_complete": True,
                        "ask_complete": True,
                    },
                    "10bps": {
                        "bid_size": "30",
                        "ask_size": "32",
                        "bid_complete": True,
                        "ask_complete": True,
                    },
                    "25bps": {
                        "bid_size": "60",
                        "ask_size": "63",
                        "bid_complete": True,
                        "ask_complete": True,
                    },
                },
            },
        )

        # This is the MCP-facing guarantee: no Decimal or float survives.
        encoded = json.dumps(brief, allow_nan=False, sort_keys=True)
        self.assertIn('"funding_hourly": "0.0000125"', encoded)
        self.assertFalse(any(isinstance(value, float) for value in _walk(brief)))

    def test_testnet_selects_only_the_compiled_in_testnet_endpoint(self) -> None:
        transport = FixtureTransport()

        brief = fixture_brief("ETH", "testnet", transport)

        self.assertEqual(brief["network"], "testnet")
        self.assertEqual(
            [endpoint for endpoint, _ in transport.calls],
            [TESTNET_INFO, TESTNET_INFO],
        )
        self.assertEqual(
            [payload["type"] for _, payload in transport.calls],
            ["metaAndAssetCtxs", "l2Book"],
        )

    def test_invalid_arguments_fail_before_transport(self) -> None:
        transport = FixtureTransport()
        cases = (
            ("ETH", "devnet"),
            ("ETH", "MAINNET"),
            (" ETH", "mainnet"),
            ("ETH/USDC", "mainnet"),
            ("", "mainnet"),
        )

        for symbol, network in cases:
            with self.subTest(symbol=symbol, network=network):
                with self.assertRaises(ValidationError):
                    fixture_brief(symbol, network, transport)

        self.assertEqual(transport.calls, [])

    def test_unknown_symbol_fails_without_fetching_a_book(self) -> None:
        transport = FixtureTransport()

        with self.assertRaisesRegex(MarketDataResponseError, "not uniquely present"):
            fixture_brief("SOL", "mainnet", transport)

        self.assertEqual(len(transport.calls), 1)

    def test_transport_exception_is_wrapped_without_leaking_its_message(self) -> None:
        def broken_transport(endpoint: str, payload: object) -> object:
            del endpoint, payload
            raise RuntimeError("private integration details")

        with self.assertRaises(MarketDataTransportError) as caught:
            fixture_brief("ETH", "mainnet", broken_transport)

        self.assertNotIn("private integration details", str(caught.exception))


class StrictResponseValidationTests(unittest.TestCase):
    def test_rejects_non_string_non_finite_and_out_of_range_market_fields(self) -> None:
        mutations: tuple[tuple[str, object], ...] = (
            ("midPx", 3000.5),
            ("markPx", "NaN"),
            ("oraclePx", "0"),
            ("funding", "1e-999"),
            ("openInterest", "-1"),
            ("dayNtlVlm", "Infinity"),
        )

        for field, invalid in mutations:
            with self.subTest(field=field, invalid=invalid):
                context = valid_context_response()
                context[1][1][field] = invalid  # type: ignore[index]
                transport = FixtureTransport(context=context)
                with self.assertRaises(MarketDataResponseError):
                    fixture_brief("ETH", "mainnet", transport)
                self.assertEqual(len(transport.calls), 1)

    def test_rejects_unaligned_or_duplicate_metadata(self) -> None:
        unaligned = valid_context_response()
        unaligned[1].pop()  # type: ignore[union-attr]

        duplicate = valid_context_response()
        duplicate[0]["universe"][0]["name"] = "eth"  # type: ignore[index]

        for response in (unaligned, duplicate):
            with self.subTest(response=response):
                with self.assertRaises(MarketDataResponseError):
                    fixture_brief("ETH", "mainnet", FixtureTransport(context=response))

    def test_rejects_invalid_books(self) -> None:
        cases: dict[str, dict[str, object]] = {}

        wrong_coin = valid_book_response()
        wrong_coin["coin"] = "BTC"
        cases["wrong coin"] = wrong_coin

        bool_time = valid_book_response()
        bool_time["time"] = True
        cases["boolean time"] = bool_time

        empty_side = valid_book_response()
        empty_side["levels"][0] = []  # type: ignore[index]
        cases["empty bids"] = empty_side

        unsorted_bids = valid_book_response()
        unsorted_bids["levels"][0][1]["px"] = "3000.5"  # type: ignore[index]
        cases["unsorted bids"] = unsorted_bids

        crossed = valid_book_response()
        crossed["levels"][0][0]["px"] = "3002"  # type: ignore[index]
        cases["crossed"] = crossed

        float_size = valid_book_response()
        float_size["levels"][1][0]["sz"] = 11.0  # type: ignore[index]
        cases["float size"] = float_size

        zero_orders = valid_book_response()
        zero_orders["levels"][1][0]["n"] = 0  # type: ignore[index]
        cases["zero order count"] = zero_orders

        for label, book in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(MarketDataResponseError):
                    fixture_brief("ETH", "mainnet", FixtureTransport(book=book))

    def test_rejects_non_json_response_shapes(self) -> None:
        cases: tuple[object, ...] = (
            None,
            {},
            [valid_context_response()[0]],
            [{"universe": "ETH"}, []],
        )

        for context in cases:
            with self.subTest(context=context):
                with self.assertRaises(MarketDataResponseError):
                    fixture_brief("ETH", "mainnet", FixtureTransport(context=context))


class MidConsistencyTests(unittest.TestCase):
    def test_rejects_context_and_book_mids_over_twenty_five_bps_apart(self) -> None:
        context = valid_context_response()
        context[1][1]["midPx"] = "3010"  # type: ignore[index]

        with self.assertRaisesRegex(MarketDataResponseError, "diverge.*25 bps"):
            fixture_brief("ETH", "mainnet", FixtureTransport(context=context))

    def test_exactly_twenty_five_bps_is_available(self) -> None:
        context = valid_context_response()
        context[1][1]["midPx"] = "3008.5025"  # type: ignore[index]

        brief = fixture_brief("ETH", "mainnet", FixtureTransport(context=context))

        consistency = brief["mid_consistency"]
        self.assertEqual(consistency["divergence_bps"], "25")  # type: ignore[index]
        self.assertEqual(consistency["max_divergence_bps"], "25")  # type: ignore[index]
        self.assertTrue(consistency["within_limit"])  # type: ignore[index]


class DepthCompletenessTests(unittest.TestCase):
    def test_full_twenty_level_sides_flag_potentially_truncated_bands(self) -> None:
        context = valid_context_response()
        context[1][1]["midPx"] = "100.005"  # type: ignore[index]
        book = valid_book_response()
        book["levels"] = [
            [
                {
                    "px": format(Decimal("100") - Decimal(index) / 100, "f"),
                    "sz": "1",
                    "n": 1,
                }
                for index in range(20)
            ],
            [
                {
                    "px": format(Decimal("100.01") + Decimal(index) / 50, "f"),
                    "sz": "1",
                    "n": 1,
                }
                for index in range(20)
            ],
        ]

        brief = fixture_brief(
            "ETH",
            "mainnet",
            FixtureTransport(context=context, book=book),
        )
        returned_book = brief["book"]
        self.assertEqual(returned_book["bid_level_count"], 20)  # type: ignore[index]
        self.assertEqual(returned_book["ask_level_count"], 20)  # type: ignore[index]
        self.assertEqual(returned_book["level_cap_per_side"], 20)  # type: ignore[index]
        depth = returned_book["depth"]  # type: ignore[index]
        self.assertTrue(depth["5bps"]["bid_complete"])
        self.assertTrue(depth["5bps"]["ask_complete"])
        self.assertTrue(depth["10bps"]["bid_complete"])
        self.assertTrue(depth["10bps"]["ask_complete"])
        self.assertFalse(depth["25bps"]["bid_complete"])
        self.assertTrue(depth["25bps"]["ask_complete"])

    def test_more_than_twenty_levels_is_rejected_as_schema_drift(self) -> None:
        book = valid_book_response()
        book["levels"][0] = [  # type: ignore[index]
            {
                "px": str(3000 - index),
                "sz": "1",
                "n": 1,
            }
            for index in range(21)
        ]

        with self.assertRaisesRegex(MarketDataResponseError, "20-level"):
            fixture_brief("ETH", "mainnet", FixtureTransport(book=book))


class FreshnessTests(unittest.TestCase):
    def test_injected_clock_distinguishes_context_and_book_receipts(self) -> None:
        calls = 0
        context_received = datetime(
            2024, 3, 9, 16, 0, 0, 200_000, tzinfo=timezone.utc
        )

        def clock() -> datetime:
            nonlocal calls
            calls += 1
            return context_received if calls == 1 else RECEIVED_AT

        brief = get_market_brief(
            "ETH",
            "mainnet",
            FixtureTransport(),
            clock=clock,
        )

        self.assertEqual(calls, 2)
        self.assertEqual(
            brief["timestamps"]["market_context"]["received_at"],  # type: ignore[index]
            "2024-03-09T16:00:00.200Z",
        )
        self.assertEqual(brief["received_at"], "2024-03-09T16:00:00.500Z")
        self.assertEqual(brief["age_ms"], 377)

    def test_rejects_a_book_older_than_sixty_seconds(self) -> None:
        stale = valid_book_response()
        stale["time"] = 1_710_000_000_500 - 60_001

        with self.assertRaisesRegex(MarketDataResponseError, "stale"):
            fixture_brief("ETH", "mainnet", FixtureTransport(book=stale))

    def test_rejects_a_book_more_than_five_seconds_in_the_future(self) -> None:
        future = valid_book_response()
        future["time"] = 1_710_000_000_500 + 5_001

        with self.assertRaisesRegex(MarketDataResponseError, "future-dated"):
            fixture_brief("ETH", "mainnet", FixtureTransport(book=future))

    def test_tolerated_clock_skew_has_zero_age(self) -> None:
        future = valid_book_response()
        future["time"] = 1_710_000_000_500 + 5_000

        brief = fixture_brief("ETH", "mainnet", FixtureTransport(book=future))

        self.assertEqual(brief["age_ms"], 0)

    def test_clock_must_return_a_timezone_aware_datetime(self) -> None:
        invalid_clocks = (
            lambda: "2024-03-09T16:00:00Z",
            lambda: datetime(2024, 3, 9, 16, 0),
        )

        for clock in invalid_clocks:
            with self.subTest(clock=clock):
                with self.assertRaisesRegex(ValidationError, "clock must return"):
                    get_market_brief(
                        "ETH",
                        "mainnet",
                        FixtureTransport(),
                        clock=clock,  # type: ignore[arg-type]
                    )


class DefaultTransportBoundaryTests(unittest.TestCase):
    def test_redirect_is_denied_before_a_second_request(self) -> None:
        attempted_urls: list[str] = []

        class RedirectingOpener:
            def __init__(self, handler: object) -> None:
                self.handler = handler

            def open(self, request: object, *, timeout: int) -> object:
                del timeout
                attempted_urls.append(request.full_url)  # type: ignore[attr-defined]
                return self.handler.redirect_request(  # type: ignore[attr-defined]
                    request,
                    None,
                    307,
                    "Temporary Redirect",
                    {"Location": "http://169.254.169.254/latest/meta-data"},
                    "http://169.254.169.254/latest/meta-data",
                )

        def build_opener(*handlers: object) -> RedirectingOpener:
            self.assertEqual(len(handlers), 2)
            self.assertIsInstance(handlers[0], market_data.urlrequest.ProxyHandler)
            self.assertEqual(handlers[0].proxies, {})  # type: ignore[attr-defined]
            self.assertIsInstance(handlers[1], market_data._RejectRedirectHandler)
            return RedirectingOpener(handlers[1])

        with mock.patch.object(
            market_data.urlrequest,
            "build_opener",
            side_effect=build_opener,
        ):
            with self.assertRaisesRegex(
                MarketDataTransportError,
                "forbidden redirect",
            ):
                market_data._default_transport(
                    MAINNET_INFO,
                    {"type": "metaAndAssetCtxs"},
                )

        self.assertEqual(attempted_urls, [MAINNET_INFO])

    def test_pathological_json_errors_are_wrapped_without_content_leakage(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                del args

            def geturl(self) -> str:
                return MAINNET_INFO

            def read(self, limit: int) -> bytes:
                del limit
                return b"{}"

        class FakeOpener:
            def open(self, request: object, *, timeout: int) -> FakeResponse:
                del request, timeout
                return FakeResponse()

        failures = (
            ValueError("response body should not leak"),
            RecursionError("response body should not leak"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(
                        market_data.urlrequest,
                        "build_opener",
                        return_value=FakeOpener(),
                    ),
                    mock.patch.object(
                        market_data.json,
                        "loads",
                        side_effect=failure,
                    ),
                ):
                    with self.assertRaises(MarketDataResponseError) as caught:
                        market_data._default_transport(
                            MAINNET_INFO,
                            {"type": "metaAndAssetCtxs"},
                        )

                self.assertNotIn("response body", str(caught.exception))


def _walk(value: object) -> list[object]:
    if isinstance(value, dict):
        values: list[object] = []
        for key, item in value.items():
            values.extend(_walk(key))
            values.extend(_walk(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_walk(item))
        return values
    return [value]


if __name__ == "__main__":
    unittest.main()
