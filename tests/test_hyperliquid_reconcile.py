from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import hashlib
import json
import unittest

from trading_harness.execution_store import LegReconciliation, VenueFill
from trading_harness.hyperliquid_account import OrderSide
from trading_harness.hyperliquid_reconcile import (
    USER_FILLS_PAGE_LIMIT,
    USER_FILLS_RETENTION_LIMIT,
    HyperliquidReconcileResponseError,
    HyperliquidReconcileTransportError,
    OwnedLeg,
    reconcile_hyperliquid_venue,
)
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from tests.test_hyperliquid_account import (
    ACCOUNT,
    RECEIVED_TIME_MS,
    SERVER_TIME_MS,
    STOP_CLOID,
    TARGET_CLOID,
    FixtureTransport as AccountTransport,
    fetch as fetch_account,
    raw_order,
    raw_position,
    valid_clearing,
    valid_orders,
)


TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
ENTRY_CLOID = "0x" + "1" * 32
PLAN_HASH = hashlib.sha256(b"plan").hexdigest()
COMMAND_ID = "command-1"
ACCOUNT_ID = "desk-testnet"
START_MS = SERVER_TIME_MS - 60_000
FILL_TIME_MS = SERVER_TIME_MS - 20_000
NOW = datetime.fromtimestamp(RECEIVED_TIME_MS / 1000, tz=timezone.utc)


def legs(*, short: bool = False) -> tuple[OwnedLeg, ...]:
    entry_side = OrderSide.SELL if short else OrderSide.BUY
    exit_side = OrderSide.BUY if short else OrderSide.SELL
    return (
        OwnedLeg("entry", ENTRY_CLOID, "ETH", entry_side, Decimal("0.5")),
        OwnedLeg(
            "protective_stop",
            STOP_CLOID,
            "ETH",
            exit_side,
            Decimal("0.5"),
        ),
        OwnedLeg(
            "take_profit",
            TARGET_CLOID,
            "ETH",
            exit_side,
            Decimal("0.5"),
        ),
    )


def order_status(
    leg: OwnedLeg,
    *,
    status: str,
    oid: int,
    remaining: str,
    cloid: str | None = None,
    status_time: int = SERVER_TIME_MS - 1_000,
) -> dict[str, object]:
    trigger = leg.role != "entry"
    trigger_price = "2400" if leg.role == "protective_stop" else "3000"
    if not trigger:
        trigger_price = "0"
    order_type = {
        "entry": "Market",
        "protective_stop": "Stop Market",
        "take_profit": "Take Market",
    }[leg.role]
    return {
        "status": "order",
        "order": {
            "order": {
                "coin": leg.symbol,
                "side": leg.side.wire_value,
                "limitPx": "2500",
                "sz": remaining,
                "oid": oid,
                "timestamp": SERVER_TIME_MS - 30_000,
                "triggerCondition": "N/A" if not trigger else "venue trigger",
                "isTrigger": trigger,
                "triggerPx": trigger_price,
                "children": [],
                "isPositionTpsl": False,
                "reduceOnly": trigger,
                "orderType": order_type,
                "origSz": "0.5",
                "tif": "Ioc" if not trigger else "FrontendMarket",
                "cloid": leg.cloid if cloid is None else cloid,
            },
            "status": status,
            "statusTimestamp": status_time,
        },
    }


def raw_fill(
    *,
    oid: int = 100,
    tid: int = 1,
    side: str = "B",
    size: str = "0.5",
    start_position: str = "0",
    time_ms: int = FILL_TIME_MS,
    symbol: str = "ETH",
    direction: str = "deliberately ignored display text",
) -> dict[str, object]:
    return {
        "closedPnl": "0",
        "coin": symbol,
        "crossed": True,
        "dir": direction,
        "hash": "0x" + f"{tid:064x}",
        "oid": oid,
        "px": "2500",
        "side": side,
        "startPosition": start_position,
        "sz": size,
        "time": time_ms,
        "fee": "0.25",
        "feeToken": "USDC",
        "tid": tid,
    }


def account_snapshot(
    *,
    short: bool = False,
    flat: bool = False,
    foreign_order: bool = False,
):
    if flat:
        clearing = valid_clearing(positions=[])
        orders: list[object] = []
    elif short:
        clearing = valid_clearing(positions=[raw_position(signed_size="-0.5")])
        orders = [
            raw_order(side="B"),
            raw_order(
                oid=102,
                cloid=TARGET_CLOID,
                side="B",
                order_type="Take Market",
                trigger_price="2000",
                trigger_condition="Triggered below 2000",
            ),
        ]
    else:
        clearing = valid_clearing()
        orders = valid_orders()
    if foreign_order:
        manual = raw_order(oid=999, cloid=None)
        del manual["cloid"]
        orders.append(manual)
    result, _ = fetch_account(
        AccountTransport(clearing=clearing, orders=orders),
        received_at_ms=RECEIVED_TIME_MS,
        network="testnet",
    )
    return result


class ReconcileTransport:
    def __init__(
        self,
        statuses: dict[str, object],
        fill_pages: list[object],
    ) -> None:
        self.statuses = statuses
        self.fill_pages = list(fill_pages)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fill_call = 0

    def __call__(self, endpoint: str, payload: Mapping[str, object]) -> object:
        request = deepcopy(dict(payload))
        self.calls.append((endpoint, request))
        if request["type"] == "orderStatus":
            return deepcopy(self.statuses[request["oid"]])  # type: ignore[index]
        if request["type"] == "userFillsByTime":
            page = self.fill_pages[self.fill_call]
            self.fill_call += 1
            return deepcopy(page)
        raise AssertionError(f"unexpected request: {request!r}")


def full_statuses(*, short: bool = False) -> dict[str, object]:
    owned = legs(short=short)
    return {
        ENTRY_CLOID: order_status(
            owned[0], status="filled", oid=100, remaining="0"
        ),
        STOP_CLOID: order_status(
            owned[1], status="open", oid=101, remaining="0.5"
        ),
        TARGET_CLOID: order_status(
            owned[2], status="open", oid=102, remaining="0.5"
        ),
    }


def reconcile(
    transport: ReconcileTransport,
    *,
    selected_snapshot=None,
    short: bool = False,
):
    return reconcile_hyperliquid_venue(
        account_snapshot() if selected_snapshot is None else selected_snapshot,
        legs(short=short),
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        plan_hash=PLAN_HASH,
        network=HyperliquidNetwork.TESTNET,
        fills_start_time_ms=START_MS,
        transport=transport,
        clock=lambda: NOW,
    )


class CompleteReconciliationTests(unittest.TestCase):
    def test_full_long_produces_exact_store_ready_protected_evidence(self) -> None:
        transport = ReconcileTransport(full_statuses(), [[raw_fill()]])

        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            bundle = reconcile(transport)

        self.assertEqual(
            transport.calls[:3],
            [
                (
                    TESTNET_INFO,
                    {"type": "orderStatus", "user": ACCOUNT, "oid": ENTRY_CLOID},
                ),
                (
                    TESTNET_INFO,
                    {"type": "orderStatus", "user": ACCOUNT, "oid": STOP_CLOID},
                ),
                (
                    TESTNET_INFO,
                    {"type": "orderStatus", "user": ACCOUNT, "oid": TARGET_CLOID},
                ),
            ],
        )
        self.assertEqual(
            transport.calls[3],
            (
                TESTNET_INFO,
                {
                    "type": "userFillsByTime",
                    "user": ACCOUNT,
                    "startTime": START_MS,
                    "endTime": SERVER_TIME_MS,
                    "aggregateByTime": False,
                },
            ),
        )
        self.assertTrue(bundle.complete)
        self.assertEqual(bundle.incomplete_reasons, ())
        self.assertEqual(bundle.signed_position_quantity, Decimal("0.5"))
        self.assertEqual(bundle.protected_quantity, Decimal("0.5"))
        self.assertEqual(
            [(item.role, item.status, item.cumulative_filled, item.venue_oid) for item in bundle.legs],
            [
                ("entry", "filled", Decimal("0.5"), 100),
                ("protective_stop", "resting", Decimal("0"), 101),
                ("take_profit", "resting", Decimal("0"), 102),
            ],
        )
        self.assertIsInstance(bundle.legs[0], LegReconciliation)
        self.assertIsInstance(bundle.fills[0], VenueFill)
        fill = bundle.signed_fills[0]
        self.assertIs(fill.side, OrderSide.BUY)
        self.assertEqual(fill.signed_quantity, Decimal("0.5"))
        self.assertEqual(fill.start_position, Decimal("0"))
        self.assertEqual(fill.end_position, Decimal("0.5"))
        self.assertEqual(fill.role, "entry")
        self.assertRegex(bundle.reconciliation_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(bundle.execution_store_kwargs()),
            {
                "account_snapshot_hash",
                "observed_at",
                "complete",
                "legs",
                "signed_position_quantity",
                "protected_quantity",
                "fills",
            },
        )
        json.dumps(bundle.as_dict(), allow_nan=False, sort_keys=True)

    def test_fully_unfilled_canceled_bracket_can_reconcile_flat(self) -> None:
        selected_legs = legs()
        statuses = {
            leg.cloid: order_status(
                leg,
                status="canceled",
                oid=100 + index,
                remaining="0.5",
            )
            for index, leg in enumerate(selected_legs)
        }
        bundle = reconcile(
            ReconcileTransport(statuses, [[]]),
            selected_snapshot=account_snapshot(flat=True),
        )

        self.assertTrue(bundle.complete)
        self.assertEqual(bundle.signed_position_quantity, Decimal("0"))
        self.assertEqual(bundle.protected_quantity, Decimal("0"))
        self.assertEqual([item.status for item in bundle.legs], ["canceled"] * 3)
        self.assertEqual(bundle.fills, ())

    def test_full_short_uses_sell_fill_math_and_verified_buy_stop(self) -> None:
        transport = ReconcileTransport(
            full_statuses(short=True),
            [[raw_fill(side="A", start_position="0")]],
        )
        bundle = reconcile(
            transport,
            selected_snapshot=account_snapshot(short=True),
            short=True,
        )

        self.assertTrue(bundle.complete)
        self.assertEqual(bundle.signed_position_quantity, Decimal("-0.5"))
        self.assertEqual(bundle.protected_quantity, Decimal("0.5"))
        self.assertEqual(bundle.signed_fills[0].signed_quantity, Decimal("-0.5"))
        self.assertEqual(bundle.signed_fills[0].end_position, Decimal("-0.5"))

    def test_current_terminal_cancel_and_reject_statuses_map_to_store_states(self) -> None:
        selected = legs()
        statuses = {
            ENTRY_CLOID: order_status(
                selected[0], status="scheduledCancel", oid=100, remaining="0.5"
            ),
            STOP_CLOID: order_status(
                selected[1], status="siblingFilledCanceled", oid=101, remaining="0.5"
            ),
            TARGET_CLOID: order_status(
                selected[2], status="badTriggerPxRejected", oid=102, remaining="0.5"
            ),
        }
        bundle = reconcile(
            ReconcileTransport(statuses, [[]]),
            selected_snapshot=account_snapshot(flat=True),
        )

        self.assertTrue(bundle.complete)
        self.assertEqual(
            [item.status for item in bundle.legs],
            ["canceled", "canceled", "rejected"],
        )


class IncompleteAndForeignStateTests(unittest.TestCase):
    def test_partial_entry_without_live_stop_is_explicitly_incomplete(self) -> None:
        selected_legs = legs()
        statuses = {
            ENTRY_CLOID: order_status(
                selected_legs[0], status="canceled", oid=100, remaining="0.3"
            ),
            STOP_CLOID: order_status(
                selected_legs[1], status="canceled", oid=101, remaining="0.5"
            ),
            TARGET_CLOID: order_status(
                selected_legs[2], status="canceled", oid=102, remaining="0.5"
            ),
        }
        partial_snapshot, _ = fetch_account(
            AccountTransport(
                clearing=valid_clearing(
                    positions=[
                        raw_position(
                            signed_size="0.2",
                            position_value="500",
                        )
                    ]
                ),
                orders=[],
            ),
            received_at_ms=RECEIVED_TIME_MS,
            network="testnet",
        )
        bundle = reconcile(
            ReconcileTransport(
                statuses,
                [[raw_fill(size="0.2", start_position="0")]],
            ),
            selected_snapshot=partial_snapshot,
        )

        self.assertFalse(bundle.complete)
        self.assertEqual(bundle.signed_position_quantity, Decimal("0.2"))
        self.assertEqual(bundle.protected_quantity, Decimal("0"))
        self.assertIn("position_not_fully_protected", bundle.incomplete_reasons)
        self.assertEqual(bundle.legs[0].cumulative_filled, Decimal("0.2"))

    def test_missing_order_is_absent_but_never_complete(self) -> None:
        statuses = full_statuses()
        statuses[STOP_CLOID] = {"status": "unknownOid"}
        bundle = reconcile(ReconcileTransport(statuses, [[raw_fill()]]))

        self.assertFalse(bundle.complete)
        self.assertIn("protective_stop_order_missing", bundle.incomplete_reasons)
        self.assertEqual(bundle.legs[1].status, "absent")
        self.assertEqual(bundle.protected_quantity, Decimal("0"))

    def test_foreign_open_order_or_unmatched_account_fill_prevents_complete(self) -> None:
        foreign_snapshot = account_snapshot(foreign_order=True)
        bundle = reconcile(
            ReconcileTransport(
                full_statuses(),
                [[raw_fill(), raw_fill(oid=999, tid=2, size="0.1")]],
            ),
            selected_snapshot=foreign_snapshot,
        )

        self.assertFalse(bundle.complete)
        self.assertIn("foreign_open_orders", bundle.incomplete_reasons)
        self.assertIn("unmatched_account_fills", bundle.incomplete_reasons)
        self.assertEqual(bundle.fill_coverage.unmatched_fills, 1)

    def test_nested_child_never_counts_as_live_stop_coverage(self) -> None:
        stop_child = raw_order(oid=101, cloid=STOP_CLOID)
        parent = raw_order(
            oid=201,
            cloid=ENTRY_CLOID,
            side="B",
            order_type="Limit",
            is_trigger=False,
            reduce_only=False,
            trigger_price="0",
            trigger_condition="N/A",
            children=[stop_child],
        )
        nested_snapshot, _ = fetch_account(
            AccountTransport(orders=[parent]),
            received_at_ms=RECEIVED_TIME_MS,
            network="testnet",
        )
        bundle = reconcile(
            ReconcileTransport(full_statuses(), [[raw_fill()]]),
            selected_snapshot=nested_snapshot,
        )

        self.assertFalse(bundle.complete)
        self.assertEqual(bundle.protected_quantity, Decimal("0"))
        self.assertIn("position_not_fully_protected", bundle.incomplete_reasons)


class FillPaginationTests(unittest.TestCase):
    def test_duplicate_timestamp_fills_are_distinct_and_boundary_duplicate_dedupes(self) -> None:
        first = raw_fill(tid=10, size="0.2", start_position="0")
        second = raw_fill(tid=11, size="0.3", start_position="0.2")
        # A short page is enough to validate same-timestamp identities and an
        # exact duplicate in one response; inclusive multi-page behavior is
        # exercised by the saturated-page test below.
        bundle = reconcile(
            ReconcileTransport(full_statuses(), [[first, second, deepcopy(first)]])
        )

        self.assertTrue(bundle.complete)
        self.assertEqual(bundle.fill_coverage.unique_fills, 2)
        self.assertEqual(bundle.fill_coverage.duplicate_fills, 1)
        self.assertEqual(len(bundle.fills), 2)
        self.assertEqual(bundle.legs[0].cumulative_filled, Decimal("0.5"))

    def test_fill_start_position_chain_is_evidence_not_display_direction(self) -> None:
        first = raw_fill(tid=20, size="0.2", start_position="0")
        discontinuous = raw_fill(tid=21, size="0.3", start_position="0.1")
        bundle = reconcile(
            ReconcileTransport(full_statuses(), [[first, discontinuous]])
        )

        self.assertFalse(bundle.complete)
        self.assertIn(
            "fill_position_chain_discontinuous",
            bundle.incomplete_reasons,
        )
        self.assertEqual(bundle.signed_fills[1].start_position, Decimal("0.1"))
        self.assertEqual(bundle.signed_fills[1].signed_quantity, Decimal("0.3"))

    def test_full_page_uses_inclusive_cursor_and_deduplicates_boundary(self) -> None:
        unrelated = [
            raw_fill(
                oid=999,
                tid=1_000 + index,
                size="0.001",
                time_ms=START_MS + index,
            )
            for index in range(USER_FILLS_PAGE_LIMIT - 1)
        ]
        first_target = raw_fill(
            tid=10,
            size="0.2",
            start_position="0",
            time_ms=START_MS + USER_FILLS_PAGE_LIMIT,
        )
        second_target = raw_fill(
            tid=11,
            size="0.3",
            start_position="0.2",
            time_ms=START_MS + USER_FILLS_PAGE_LIMIT + 1,
        )
        transport = ReconcileTransport(
            full_statuses(),
            [[*unrelated, first_target], [deepcopy(first_target), second_target]],
        )
        bundle = reconcile(transport)

        fill_calls = [payload for _, payload in transport.calls if payload["type"] == "userFillsByTime"]
        self.assertEqual(len(fill_calls), 2)
        self.assertEqual(
            fill_calls[1]["startTime"],
            START_MS + USER_FILLS_PAGE_LIMIT,
        )
        self.assertEqual(bundle.fill_coverage.duplicate_fills, 1)
        self.assertEqual(bundle.legs[0].cumulative_filled, Decimal("0.5"))
        self.assertFalse(bundle.complete)
        self.assertEqual(bundle.fill_coverage.unmatched_fills, len(unrelated))

    def test_inclusive_page_must_repeat_every_boundary_identity(self) -> None:
        prefix = [
            raw_fill(
                oid=999,
                tid=2_000 + index,
                size="0.001",
                time_ms=START_MS + index,
            )
            for index in range(USER_FILLS_PAGE_LIMIT - 1)
        ]
        boundary = raw_fill(
            tid=30,
            size="0.2",
            start_position="0",
            time_ms=START_MS + USER_FILLS_PAGE_LIMIT,
        )
        later = raw_fill(
            tid=31,
            size="0.3",
            start_position="0.2",
            time_ms=START_MS + USER_FILLS_PAGE_LIMIT + 1,
        )
        transport = ReconcileTransport(
            full_statuses(),
            [[*prefix, boundary], [later]],
        )

        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "overlap"
        ):
            reconcile(transport)

    def test_fill_page_must_be_ascending(self) -> None:
        later = raw_fill(tid=41, time_ms=START_MS + 2)
        earlier = raw_fill(tid=40, time_ms=START_MS + 1)
        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "ascending"
        ):
            reconcile(
                ReconcileTransport(full_statuses(), [[later, earlier]])
            )

    def test_latest_10000_fill_limit_is_explicitly_incomplete(self) -> None:
        class RetentionTransport(ReconcileTransport):
            def __init__(self) -> None:
                super().__init__(full_statuses(), [])
                self.next_tid = 1
                self.boundary = None

            def __call__(self, endpoint: str, payload: Mapping[str, object]) -> object:
                if payload["type"] == "orderStatus":
                    return super().__call__(endpoint, payload)
                request = deepcopy(dict(payload))
                self.calls.append((endpoint, request))
                page = self.fill_call
                self.fill_call += 1
                page_size = USER_FILLS_PAGE_LIMIT if page < 5 else 5
                values = (
                    [] if self.boundary is None else [deepcopy(self.boundary)]
                )
                while len(values) < page_size:
                    values.append(
                        raw_fill(
                            oid=999,
                            tid=self.next_tid,
                            size="0.001",
                            time_ms=START_MS + self.next_tid,
                        )
                    )
                    self.next_tid += 1
                self.boundary = values[-1]
                return values

        bundle = reconcile(RetentionTransport())

        self.assertFalse(bundle.complete)
        self.assertTrue(bundle.fill_coverage.retention_limited)
        self.assertEqual(
            bundle.fill_coverage.unique_fills,
            USER_FILLS_RETENTION_LIMIT,
        )
        self.assertEqual(
            bundle.fill_coverage.reason,
            "latest_10000_fill_retention_limit",
        )


class StrictStatusAndFillTests(unittest.TestCase):
    def test_unknown_status_foreign_cloid_and_future_status_are_rejected(self) -> None:
        mutations = []
        unknown = full_statuses()
        unknown[ENTRY_CLOID]["order"]["status"] = "newVenueStatus"  # type: ignore[index]
        mutations.append(unknown)
        foreign = full_statuses()
        foreign[ENTRY_CLOID]["order"]["order"]["cloid"] = "0x" + "f" * 32  # type: ignore[index]
        mutations.append(foreign)
        future = full_statuses()
        future[ENTRY_CLOID]["order"]["statusTimestamp"] = RECEIVED_TIME_MS + 1  # type: ignore[index]
        mutations.append(future)

        for statuses in mutations:
            with self.subTest(statuses=statuses):
                with self.assertRaises(HyperliquidReconcileResponseError):
                    reconcile(ReconcileTransport(statuses, [[raw_fill()]]))

    def test_float_future_conflict_and_wrong_side_fills_are_rejected(self) -> None:
        float_fill = raw_fill()
        float_fill["sz"] = 0.5
        future_fill = raw_fill(time_ms=RECEIVED_TIME_MS + 1)
        wrong_side = raw_fill(side="A")
        conflict = raw_fill()
        conflict["fee"] = "1"

        for fills in (
            [float_fill],
            [future_fill],
            [wrong_side],
            [raw_fill(), conflict],
        ):
            with self.subTest(fills=fills):
                with self.assertRaises(HyperliquidReconcileResponseError):
                    reconcile(ReconcileTransport(full_statuses(), [fills]))

    def test_transport_error_is_sanitized(self) -> None:
        def broken(endpoint: str, payload: Mapping[str, object]) -> object:
            del endpoint, payload
            raise RuntimeError("private response material")

        with self.assertRaises(HyperliquidReconcileTransportError) as caught:
            reconcile_hyperliquid_venue(
                account_snapshot(),
                legs(),
                account_id=ACCOUNT_ID,
                command_id=COMMAND_ID,
                plan_hash=PLAN_HASH,
                network=HyperliquidNetwork.TESTNET,
                fills_start_time_ms=START_MS,
                transport=broken,
                clock=lambda: NOW,
            )
        self.assertNotIn("private response material", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
