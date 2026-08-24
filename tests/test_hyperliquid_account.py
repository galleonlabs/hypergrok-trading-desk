from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import json
import unittest

from trading_harness.errors import ValidationError
from trading_harness.hyperliquid_account import (
    HyperliquidAccountResponseError,
    HyperliquidAccountTransportError,
    OrderSide,
    PositionSide,
    StaleAccountSnapshotError,
    StandardAccountMode,
    TriggerKind,
    UnsupportedAccountModeError,
    fetch_account_snapshot,
)


MAINNET_INFO = "https://api.hyperliquid.xyz/info"
TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
ACCOUNT = "0x1111111111111111111111111111111111111111"
SERVER_TIME_MS = 1_787_592_000_000
RECEIVED_TIME_MS = SERVER_TIME_MS + 500
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ENTRY_CLOID = "0x" + "1" * 32
STOP_CLOID = "0x" + "2" * 32
TARGET_CLOID = "0x" + "3" * 32


def datetime_from_ms(value: int) -> datetime:
    return EPOCH + timedelta(milliseconds=value)


def fixed_clock(value: int = RECEIVED_TIME_MS):
    def read() -> datetime:
        return datetime_from_ms(value)

    return read


def valid_meta() -> dict[str, object]:
    return {
        "universe": [
            {
                "name": "BTC",
                "szDecimals": 5,
                "maxLeverage": 40,
                "marginTableId": 56,
                "marginMode": "strictIsolated",
                "onlyIsolated": True,
                "isDelisted": True,
            },
            {
                "name": "ETH",
                "szDecimals": 4,
                "maxLeverage": 25,
                "marginTableId": 55,
            },
        ],
        "marginTables": [],
        "collateralToken": 0,
    }


def raw_position(
    *,
    symbol: str = "ETH",
    signed_size: object = "0.5000",
    entry_price: object = "2500.00",
    position_value: object = "1250.00",
    max_leverage: object = 25,
) -> dict[str, object]:
    return {
        "type": "oneWay",
        "position": {
            "coin": symbol,
            "cumFunding": {
                "allTime": "-1.2500",
                "sinceOpen": "-0.250",
                "sinceChange": "0.0500",
            },
            "entryPx": entry_price,
            "leverage": {"type": "cross", "value": 2},
            "liquidationPx": "1250.00",
            "marginUsed": "625.0",
            "maxLeverage": max_leverage,
            "positionValue": position_value,
            "returnOnEquity": "0.0200",
            "szi": signed_size,
            "unrealizedPnl": "12.500",
        },
    }


def valid_clearing(
    *,
    positions: list[object] | None = None,
    server_time: object = SERVER_TIME_MS,
) -> dict[str, object]:
    summary = {
        "accountValue": "10200.000",
        "totalNtlPos": "1250.0",
        "totalRawUsd": "8950.00",
        "totalMarginUsed": "625.0",
    }
    return {
        "marginSummary": deepcopy(summary),
        "crossMarginSummary": deepcopy(summary),
        "crossMaintenanceMarginUsed": "62.50",
        "withdrawable": "9500.000",
        "assetPositions": [raw_position()] if positions is None else positions,
        "time": server_time,
    }


def raw_order(
    *,
    oid: int = 101,
    cloid: object = STOP_CLOID,
    symbol: str = "ETH",
    side: object = "A",
    size: object = "0.5000",
    original_size: object = "0.5000",
    order_type: object = "Stop Market",
    is_trigger: object = True,
    reduce_only: object = True,
    trigger_price: object = "2400.00",
    trigger_condition: object = "Triggered below 2400",
    children: list[object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "coin": symbol,
        "isPositionTpsl": False,
        "isTrigger": is_trigger,
        "limitPx": "2160.00",
        "oid": oid,
        "orderType": order_type,
        "origSz": original_size,
        "reduceOnly": reduce_only,
        "side": side,
        "sz": size,
        "tif": "FrontendMarket",
        "timestamp": SERVER_TIME_MS - 10_000,
        "triggerCondition": trigger_condition,
        "triggerPx": trigger_price,
        "children": [] if children is None else children,
    }
    if cloid is not _MISSING:
        result["cloid"] = cloid
    return result


_MISSING = object()


def valid_orders() -> list[object]:
    return [
        raw_order(),
        raw_order(
            oid=102,
            cloid=TARGET_CLOID,
            order_type="Take Market",
            trigger_price="3000",
            trigger_condition="Triggered above 3000",
        ),
    ]


class FixtureTransport:
    def __init__(
        self,
        *,
        mode: object = "default",
        meta: object | None = None,
        clearing: object | None = None,
        orders: object | None = None,
    ) -> None:
        self.mode = mode
        self.meta = valid_meta() if meta is None else meta
        self.clearing = valid_clearing() if clearing is None else clearing
        self.orders = valid_orders() if orders is None else orders
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload: Mapping[str, object]) -> object:
        request = deepcopy(dict(payload))
        self.calls.append((endpoint, request))
        request_type = request.get("type")
        if request_type == "userAbstraction":
            return deepcopy(self.mode)
        if request_type == "meta":
            return deepcopy(self.meta)
        if request_type == "clearinghouseState":
            return deepcopy(self.clearing)
        if request_type == "frontendOpenOrders":
            return deepcopy(self.orders)
        raise AssertionError(f"unexpected request: {request!r}")


def fetch(
    transport: FixtureTransport | None = None,
    *,
    received_at_ms: int = RECEIVED_TIME_MS,
    network: str = "mainnet",
):
    selected = FixtureTransport() if transport is None else transport
    result = fetch_account_snapshot(
        ACCOUNT,
        network,
        transport=selected,
        clock=fixed_clock(received_at_ms),
    )
    return result, selected


def walk(value: object) -> list[object]:
    if isinstance(value, dict):
        result: list[object] = []
        for key, item in value.items():
            result.extend(walk(key))
            result.extend(walk(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(walk(item))
        return result
    return [value]


class SnapshotIngestionTests(unittest.TestCase):
    def test_binds_every_user_read_to_main_account_and_parses_exact_state(self) -> None:
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            snapshot, transport = fetch(network="testnet")

        self.assertEqual(
            transport.calls,
            [
                (
                    TESTNET_INFO,
                    {"type": "userAbstraction", "user": ACCOUNT},
                ),
                (TESTNET_INFO, {"type": "meta"}),
                (
                    TESTNET_INFO,
                    {"type": "clearinghouseState", "user": ACCOUNT},
                ),
                (
                    TESTNET_INFO,
                    {"type": "frontendOpenOrders", "user": ACCOUNT},
                ),
            ],
        )
        self.assertEqual(snapshot.main_account_address, ACCOUNT)
        self.assertIs(snapshot.account_mode, StandardAccountMode.DEFAULT)
        self.assertEqual(snapshot.server_time_ms, SERVER_TIME_MS)
        self.assertEqual(snapshot.age_ms, 500)
        self.assertEqual(snapshot.margin_summary.account_value, Decimal("10200.000"))
        self.assertEqual(snapshot.withdrawable, Decimal("9500.000"))
        self.assertEqual(snapshot.positions[0].signed_size, Decimal("0.5000"))
        self.assertIs(snapshot.positions[0].side, PositionSide.LONG)
        self.assertEqual(snapshot.positions[0].absolute_size, Decimal("0.5000"))
        self.assertEqual(snapshot.positions[0].asset_id, 1)
        self.assertEqual(snapshot.open_orders[0].cloid, STOP_CLOID)
        self.assertIs(snapshot.open_orders[0].side, OrderSide.SELL)
        self.assertIs(snapshot.open_orders[0].trigger_kind, TriggerKind.STOP_LOSS)
        self.assertTrue(snapshot.open_orders[0].is_protective_stop)
        self.assertRegex(snapshot.snapshot_hash, r"^[0-9a-f]{64}$")

        eth = snapshot.metadata.instrument("ETH")
        self.assertEqual(eth.asset_id, 1)
        self.assertEqual(eth.sz_decimals, 4)
        self.assertEqual(eth.max_leverage, Decimal("25"))
        self.assertEqual(eth.margin_mode, "cross")
        self.assertFalse(eth.is_delisted)
        btc = snapshot.metadata.instrument("BTC")
        self.assertEqual(btc.margin_mode, "strictIsolated")
        self.assertTrue(btc.is_delisted)

        wire = eth.to_wire_metadata()
        self.assertEqual(wire.asset_id, 1)
        self.assertEqual(wire.source_hash, eth.metadata_hash)
        self.assertEqual(wire.max_leverage, Decimal("25"))

        document = snapshot.as_dict()
        self.assertEqual(document["schema_version"], "hyperliquid.account_snapshot.v1")
        self.assertEqual(document["main_account_address"], ACCOUNT)
        self.assertTrue(document["read_only"])
        self.assertFalse(any(isinstance(value, float) for value in walk(document)))
        json.dumps(document, allow_nan=False, sort_keys=True)

    def test_disabled_mode_is_the_other_supported_standard_mode(self) -> None:
        snapshot, _ = fetch(FixtureTransport(mode="disabled"))

        self.assertIs(snapshot.account_mode, StandardAccountMode.DISABLED)

    def test_hash_is_stable_under_ordering_and_decimal_format_variants(self) -> None:
        baseline, _ = fetch()
        meta = valid_meta()
        meta["universe"][1]["maxLeverage"] = "25.0"  # type: ignore[index]
        clearing = valid_clearing()
        clearing["withdrawable"] = "9500"
        variant, _ = fetch(
            FixtureTransport(
                meta=meta,
                clearing=clearing,
                orders=list(reversed(valid_orders())),
            )
        )

        self.assertEqual(variant.snapshot_hash, baseline.snapshot_hash)
        self.assertEqual(variant.metadata.metadata_hash, baseline.metadata.metadata_hash)
        self.assertEqual(variant.open_orders, baseline.open_orders)


class AccountModeAndFreshnessTests(unittest.TestCase):
    def test_nonstandard_modes_fail_before_other_reads(self) -> None:
        for mode in ("unifiedAccount", "portfolioMargin", "dexAbstraction"):
            with self.subTest(mode=mode):
                transport = FixtureTransport(mode=mode)
                with self.assertRaises(UnsupportedAccountModeError):
                    fetch(transport)
                self.assertEqual(
                    transport.calls,
                    [
                        (
                            MAINNET_INFO,
                            {"type": "userAbstraction", "user": ACCOUNT},
                        )
                    ],
                )

    def test_unknown_or_wrong_shape_account_mode_fails_closed(self) -> None:
        for mode in ("futureMode", {"mode": "default"}, None):
            with self.subTest(mode=mode):
                with self.assertRaises(HyperliquidAccountResponseError):
                    fetch(FixtureTransport(mode=mode))

    def test_stale_and_future_server_times_are_rejected(self) -> None:
        stale = valid_clearing(server_time=SERVER_TIME_MS - 5_001)
        future = valid_clearing(server_time=RECEIVED_TIME_MS + 5_001)

        for clearing in (stale, future):
            with self.subTest(clearing=clearing):
                with self.assertRaises(StaleAccountSnapshotError):
                    fetch(FixtureTransport(clearing=clearing))

    def test_invalid_account_network_and_freshness_bounds_fail_before_transport(self) -> None:
        transport = FixtureTransport()
        cases = (
            ("0x" + "A" * 40, "mainnet", 5_000, 5_000),
            ("0x1234", "mainnet", 5_000, 5_000),
            (ACCOUNT, "MAINNET", 5_000, 5_000),
            (ACCOUNT, "mainnet", 0, 5_000),
            (ACCOUNT, "mainnet", 5_000, 60_001),
        )
        for account, network, age, skew in cases:
            with self.subTest(account=account, network=network, age=age, skew=skew):
                with self.assertRaises(ValidationError):
                    fetch_account_snapshot(
                        account,
                        network,
                        transport=transport,
                        clock=fixed_clock(),
                        maximum_age_ms=age,
                        maximum_future_skew_ms=skew,
                    )
        self.assertEqual(transport.calls, [])


class StrictVenueSchemaTests(unittest.TestCase):
    def test_metadata_rejects_unknown_fields_duplicates_and_float_leverage(self) -> None:
        unknown = valid_meta()
        unknown["universe"][0]["newField"] = True  # type: ignore[index]
        duplicate = valid_meta()
        duplicate["universe"][1]["name"] = "btc"  # type: ignore[index]
        float_leverage = valid_meta()
        float_leverage["universe"][1]["maxLeverage"] = 25.0  # type: ignore[index]
        fractional_leverage = valid_meta()
        fractional_leverage["universe"][1]["maxLeverage"] = "25.5"  # type: ignore[index]
        conflicting_margin = valid_meta()
        conflicting_margin["universe"][0]["onlyIsolated"] = False  # type: ignore[index]

        for meta in (
            unknown,
            duplicate,
            float_leverage,
            fractional_leverage,
            conflicting_margin,
        ):
            with self.subTest(meta=meta):
                with self.assertRaises(HyperliquidAccountResponseError):
                    fetch(FixtureTransport(meta=meta))

    def test_clearinghouse_requires_exact_decimal_and_position_contract(self) -> None:
        float_summary = valid_clearing()
        float_summary["marginSummary"]["accountValue"] = 10200.0  # type: ignore[index]
        zero_position = valid_clearing(positions=[raw_position(signed_size="0")])
        unknown_position = valid_clearing(positions=[raw_position(symbol="SOL")])
        extra_position = valid_clearing()
        extra_position["assetPositions"][0]["position"]["newField"] = "x"  # type: ignore[index]
        excessive_leverage = valid_clearing(
            positions=[raw_position(max_leverage=26)]
        )

        for clearing in (
            float_summary,
            zero_position,
            unknown_position,
            extra_position,
            excessive_leverage,
        ):
            with self.subTest(clearing=clearing):
                with self.assertRaises((HyperliquidAccountResponseError, ValidationError)):
                    fetch(FixtureTransport(clearing=clearing))

    def test_open_orders_reject_trigger_float_identity_and_future_drift(self) -> None:
        non_reduce_trigger = raw_order(reduce_only=False)
        float_size = raw_order(size=0.5)
        unknown_symbol = raw_order(symbol="SOL")
        bad_cloid = raw_order(cloid="0xABC")
        future = raw_order()
        future["timestamp"] = RECEIVED_TIME_MS + 5_001
        duplicate_oid = [raw_order(), raw_order(oid=101, cloid=TARGET_CLOID)]
        duplicate_cloid = [raw_order(), raw_order(oid=102, cloid=STOP_CLOID)]

        for orders in (
            [non_reduce_trigger],
            [float_size],
            [unknown_symbol],
            [bad_cloid],
            [future],
            duplicate_oid,
            duplicate_cloid,
        ):
            with self.subTest(orders=orders):
                with self.assertRaises((HyperliquidAccountResponseError, ValidationError)):
                    fetch(FixtureTransport(orders=orders))

    def test_transport_failure_is_sanitized(self) -> None:
        def broken(endpoint: str, payload: Mapping[str, object]) -> object:
            del endpoint, payload
            raise RuntimeError("private integration details")

        with self.assertRaises(HyperliquidAccountTransportError) as caught:
            fetch_account_snapshot(
                ACCOUNT,
                "mainnet",
                transport=broken,
                clock=fixed_clock(),
            )
        self.assertNotIn("private integration details", str(caught.exception))


class ReconciliationPredicateTests(unittest.TestCase):
    def test_known_full_stop_coverage_is_reconciled_without_halt(self) -> None:
        snapshot, _ = fetch()

        coverage = snapshot.protection_coverage(
            "ETH", expected_stop_cloids=(STOP_CLOID,)
        )
        reconciliation = snapshot.reconcile(
            owned_cloids=(STOP_CLOID, TARGET_CLOID),
            allowed_position_symbols=("ETH",),
            expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
        )

        self.assertEqual(coverage.required_size, Decimal("0.5"))
        self.assertEqual(coverage.covered_size, Decimal("0.5"))
        self.assertEqual(coverage.deficit_size, Decimal("0"))
        self.assertTrue(coverage.fully_protected)
        self.assertEqual(coverage.qualifying_oids, (101,))
        self.assertFalse(reconciliation.halt_required)
        self.assertEqual(reconciliation.foreign_order_oids, ())
        self.assertEqual(reconciliation.foreign_position_symbols, ())

    def test_insufficient_or_wrong_side_stop_requires_halt(self) -> None:
        for stop in (
            raw_order(size="0.20", original_size="0.20"),
            raw_order(side="B"),
        ):
            with self.subTest(stop=stop):
                snapshot, _ = fetch(FixtureTransport(orders=[stop]))
                result = snapshot.reconcile(
                    owned_cloids=(STOP_CLOID,),
                    allowed_position_symbols=("ETH",),
                    expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
                )
                self.assertTrue(result.halt_required)
                self.assertFalse(result.protection[0].fully_protected)

    def test_nested_untriggered_child_does_not_count_as_live_protection(self) -> None:
        child = raw_order(oid=202, cloid=STOP_CLOID)
        parent = raw_order(
            oid=201,
            cloid=ENTRY_CLOID,
            side="B",
            order_type="Limit",
            is_trigger=False,
            reduce_only=False,
            trigger_price="0",
            trigger_condition="N/A",
            children=[child],
        )
        snapshot, _ = fetch(FixtureTransport(orders=[parent]))

        result = snapshot.reconcile(
            owned_cloids=(ENTRY_CLOID, STOP_CLOID),
            allowed_position_symbols=("ETH",),
            expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
        )

        self.assertEqual(result.foreign_order_oids, ())
        self.assertTrue(result.halt_required)
        self.assertEqual(result.protection[0].covered_size, Decimal("0"))
        self.assertFalse(result.protection[0].fully_protected)

    def test_dynamic_position_stop_zero_size_covers_the_whole_position(self) -> None:
        dynamic_stop = raw_order(size="0", original_size="0")
        dynamic_stop["isPositionTpsl"] = True
        snapshot, _ = fetch(FixtureTransport(orders=[dynamic_stop]))

        coverage = snapshot.protection_coverage(
            "ETH", expected_stop_cloids=(STOP_CLOID,)
        )

        self.assertEqual(coverage.required_size, Decimal("0.5"))
        self.assertEqual(coverage.covered_size, Decimal("0.5"))
        self.assertTrue(coverage.fully_protected)

    def test_foreign_order_or_position_requires_halt(self) -> None:
        manual = raw_order(oid=999, cloid=_MISSING)
        foreign_order_snapshot, _ = fetch(
            FixtureTransport(orders=[*valid_orders(), manual])
        )
        order_result = foreign_order_snapshot.reconcile(
            owned_cloids=(STOP_CLOID, TARGET_CLOID),
            allowed_position_symbols=("ETH",),
            expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
        )
        self.assertEqual(order_result.foreign_order_oids, (999,))
        self.assertTrue(order_result.halt_required)

        btc = raw_position(
            symbol="BTC",
            signed_size="-0.01",
            entry_price="60000",
            position_value="600",
            max_leverage=40,
        )
        btc["position"]["leverage"] = {"type": "isolated", "value": 2, "rawUsd": "300"}  # type: ignore[index]
        foreign_position_snapshot, _ = fetch(
            FixtureTransport(clearing=valid_clearing(positions=[raw_position(), btc]))
        )
        position_result = foreign_position_snapshot.reconcile(
            owned_cloids=(STOP_CLOID, TARGET_CLOID),
            allowed_position_symbols=("ETH",),
            expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
        )
        self.assertEqual(position_result.foreign_position_symbols, ("BTC",))
        self.assertTrue(position_result.halt_required)

    def test_orphan_stop_halts_and_short_requires_buy_stop(self) -> None:
        flat, _ = fetch(
            FixtureTransport(clearing=valid_clearing(positions=[]), orders=valid_orders())
        )
        orphan = flat.reconcile(
            owned_cloids=(STOP_CLOID, TARGET_CLOID),
            allowed_position_symbols=("ETH",),
            expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
        )
        self.assertEqual(orphan.orphan_protection_oids, (101, 102))
        self.assertTrue(orphan.halt_required)

        short_clearing = valid_clearing(
            positions=[raw_position(signed_size="-0.5")]
        )
        buy_stop = raw_order(side="B")
        short, _ = fetch(
            FixtureTransport(clearing=short_clearing, orders=[buy_stop])
        )
        coverage = short.protection_coverage(
            "ETH", expected_stop_cloids=(STOP_CLOID,)
        )
        self.assertIs(coverage.position_side, PositionSide.SHORT)
        self.assertTrue(coverage.fully_protected)

    def test_expected_stop_must_be_in_owned_set(self) -> None:
        snapshot, _ = fetch()

        with self.assertRaisesRegex(ValidationError, "owned"):
            snapshot.reconcile(
                owned_cloids=(TARGET_CLOID,),
                allowed_position_symbols=("ETH",),
                expected_stop_cloids_by_symbol={"ETH": (STOP_CLOID,)},
            )


if __name__ == "__main__":
    unittest.main()
