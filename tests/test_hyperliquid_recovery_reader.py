from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from unittest import mock

from trading_harness.execution_store import RecoveryPermit
from trading_harness.hyperliquid_reconcile import (
    HyperliquidReconcileResponseError,
    VenueOrderState,
)
from trading_harness.hyperliquid_recovery import (
    CancelRequest,
    build_cancel_by_cloid,
    build_noop_fence,
    build_reduce_only_close,
    recovery_action_material,
)
from trading_harness.hyperliquid_recovery_reader import (
    HyperliquidRecoveryVenueReader,
)
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.recovery_reconciliation import RecoveryVenueRead
from trading_harness import hyperliquid_recovery_reader as reader_module
from tests.test_execution_store import ExecutionStoreTestCase, NOW, digest
from tests.test_hyperliquid_account import (
    ACCOUNT,
    FixtureTransport as AccountTransport,
    fetch as fetch_account,
    raw_order,
    raw_position,
    valid_clearing,
    valid_orders,
)


TESTNET_INFO = "https://api.hyperliquid-testnet.xyz/info"
CLOSE_CLOID = "0x" + "c" * 32
START_OFFSET_MS = 60_000
READ_AT = NOW + timedelta(seconds=12)
READ_MS = int(READ_AT.timestamp() * 1_000)
SERVER_MS = READ_MS - 500
START_MS = SERVER_MS - START_OFFSET_MS


def account_snapshot(
    *,
    at: datetime = READ_AT,
    signed_position: str | None = None,
    orders: list[object] | None = None,
):
    received_ms = int(at.timestamp() * 1_000)
    server_ms = received_ms - 500
    positions = (
        []
        if signed_position is None
        else [raw_position(signed_size=signed_position)]
    )
    snapshot, _ = fetch_account(
        AccountTransport(
            clearing=valid_clearing(
                positions=positions,
                server_time=server_ms,
            ),
            orders=[] if orders is None else orders,
        ),
        received_at_ms=received_ms,
        network="testnet",
    )
    return snapshot


def status_response(
    *,
    cloid: str,
    oid: int,
    status: str,
    remaining: str,
    original: str = "0.5",
    symbol: str = "ETH",
    side: str = "A",
    trigger: bool = False,
    reduce_only: bool = True,
    status_time_ms: int = SERVER_MS - 100,
) -> dict[str, object]:
    return {
        "status": "order",
        "order": {
            "order": {
                "coin": symbol,
                "side": side,
                "limitPx": "2400",
                "sz": remaining,
                "oid": oid,
                "timestamp": status_time_ms - 100,
                "triggerCondition": "N/A" if not trigger else "venue trigger",
                "isTrigger": trigger,
                "triggerPx": "0" if not trigger else "3000",
                "children": [],
                "isPositionTpsl": False,
                "reduceOnly": reduce_only,
                "orderType": "Market" if not trigger else "Take Market",
                "origSz": original,
                "tif": "Ioc" if not trigger else "FrontendMarket",
                "cloid": cloid,
            },
            "status": status,
            "statusTimestamp": status_time_ms,
        },
    }


def raw_fill(
    *,
    oid: int,
    tid: int,
    size: str,
    start_position: str,
    time_ms: int,
    side: str = "A",
    symbol: str = "ETH",
) -> dict[str, object]:
    return {
        "closedPnl": "0",
        "coin": symbol,
        "crossed": True,
        "dir": "display text is not trusted",
        "hash": "0x" + f"{tid:064x}",
        "oid": oid,
        "px": "2400",
        "side": side,
        "startPosition": start_position,
        "sz": size,
        "time": time_ms,
        "fee": "0.01",
        "feeToken": "USDC",
        "tid": tid,
    }


class InfoTransport:
    def __init__(
        self,
        statuses: Mapping[str, object],
        pages: list[object],
    ) -> None:
        self.statuses = dict(statuses)
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.page_index = 0

    def __call__(self, endpoint: str, payload: Mapping[str, object]) -> object:
        request = deepcopy(dict(payload))
        self.calls.append((endpoint, request))
        if request.get("type") == "orderStatus":
            return deepcopy(
                self.statuses.get(request["oid"], {"status": "unknownOid"})
            )
        if request.get("type") == "userFillsByTime":
            result = self.pages[self.page_index]
            self.page_index += 1
            return deepcopy(result)
        raise AssertionError(f"unexpected request: {request!r}")


class RecoveryVenueReaderTests(ExecutionStoreTestCase):
    def _incident(self, *, unknown: bool = False):
        if unknown:
            self.prepare_unknown()
        else:
            self.admit_one()
        return self.store.record_incident(
            incident_id="reader-incident",
            command_id="command-1",
            code="RECOVERY_REQUIRED",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )

    def _queue(self, action):
        material = recovery_action_material(action)
        permit = RecoveryPermit(
            permit_id=f"reader-permit-{action.kind.value}",
            token_hash=digest(f"reader-token-{action.kind.value}"),
            parent_command_id="command-1",
            incident_id=action.incident_id,
            kind=action.kind.value,
            environment=self.store.environment,
            account_id=self.store.account_id,
            source_hash=(
                action.ambiguous_attempt_hash
                if action.kind.value == "noop_fence"
                else digest(f"reader-source-{action.kind.value}")
            ),
            preflight_hash=(
                action.preflight_hash if action.kind.value == "noop_fence" else None
            ),
            recovery_hash=action.recovery_hash,
            recovery_material=material,
            safety_policy_hash=digest("reader-safety-policy"),
            original_attempt_id=(
                action.attempt_id if action.kind.value == "noop_fence" else None
            ),
            original_nonce=(
                action.original_nonce if action.kind.value == "noop_fence" else None
            ),
            issuer_id="reader-safety-authority",
            audience="reader-recovery-worker",
            issued_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(seconds=16),
        )
        self.store.register_recovery_permit(permit)
        return self.store.queue_recovery(
            recovery_command_id=f"reader-command-{action.kind.value}",
            permit_id=permit.permit_id,
            token_hash=permit.token_hash,
            audience=permit.audience,
            at=NOW + timedelta(seconds=7),
        )

    def _close_command(self):
        incident = self._incident()
        action = build_reduce_only_close(
            account_snapshot(
                at=NOW + timedelta(seconds=6), signed_position="0.5"
            ),
            symbol="ETH",
            price_bound=Decimal("2400"),
            cloid=CLOSE_CLOID,
            incident=incident,
            account_id=self.store.account_id,
            network=HyperliquidNetwork.TESTNET,
            at=NOW + timedelta(seconds=6),
        )
        return self._queue(action)

    def _cancel_command(self):
        incident = self._incident()
        initial = account_snapshot(
            at=NOW + timedelta(seconds=6),
            signed_position="0.5",
            orders=valid_orders(),
        )
        target_cloid = valid_orders()[1]["cloid"]
        action = build_cancel_by_cloid(
            initial,
            (CancelRequest("ETH", target_cloid),),
            owned_cloids=tuple(
                order["cloid"] for order in valid_orders()  # type: ignore[index]
            ),
            incident=incident,
            account_id=self.store.account_id,
            network=HyperliquidNetwork.TESTNET,
            at=NOW + timedelta(seconds=6),
        )
        return self._queue(action), target_cloid

    def _noop_command(self):
        incident = self._incident(unknown=True)
        attempt = self.store.get_attempt("command-1")
        action = build_noop_fence(
            attempt,
            incident=incident,
            account_id=self.store.account_id,
            main_account_address=ACCOUNT,
            network=HyperliquidNetwork.TESTNET,
            at=NOW + timedelta(seconds=6),
        )
        return self._queue(action)

    def _read(self, command, snapshot, transport):
        return HyperliquidRecoveryVenueReader(
            self.store,
            transport=transport,
            clock=lambda: READ_AT,
        ).read(
            command,
            snapshot,
            fills_start_time_ms=int(snapshot.server_time_ms - START_OFFSET_MS),
        )

    def test_filled_partial_and_unfilled_reduce_only_close(self) -> None:
        cases = (
            (
                "filled",
                "0",
                None,
                [
                    raw_fill(
                        oid=501,
                        tid=1,
                        size="0.5",
                        start_position="0.5",
                        time_ms=SERVER_MS - 50,
                    )
                ],
                1,
            ),
            (
                "open",
                "0.3",
                "0.3",
                [
                    raw_fill(
                        oid=501,
                        tid=2,
                        size="0.2",
                        start_position="0.5",
                        time_ms=SERVER_MS - 50,
                    )
                ],
                1,
            ),
            ("open", "0.5", "0.5", [], 0),
        )
        for status, remaining, position, fills, expected_fills in cases:
            with self.subTest(status=status, remaining=remaining):
                self.tearDown()
                self.setUp()
                command = self._close_command()
                snapshot = account_snapshot(signed_position=position)
                transport = InfoTransport(
                    {
                        CLOSE_CLOID: status_response(
                            cloid=CLOSE_CLOID,
                            oid=501,
                            status=status,
                            remaining=remaining,
                        )
                    },
                    [fills],
                )

                result = self._read(command, snapshot, transport)

                self.assertIsInstance(result, RecoveryVenueRead)
                self.assertEqual(result.order_statuses[0].venue_status, status)
                self.assertEqual(
                    result.order_statuses[0].remaining_size,
                    Decimal(remaining),
                )
                self.assertEqual(len(result.signed_fills), expected_fills)
                self.assertTrue(result.fill_coverage.complete)
                self.assertEqual(
                    transport.calls[0],
                    (
                        TESTNET_INFO,
                        {"type": "orderStatus", "user": ACCOUNT, "oid": CLOSE_CLOID},
                    ),
                )

    def test_parent_stop_and_recovery_close_share_one_exact_fill_chain(self) -> None:
        command = self._close_command()
        legs = {item.role: item for item in self.store.get_legs("command-1")}
        stop = legs["protective_stop"]
        stop_size = stop.requested_quantity
        residual = Decimal("0.5") - stop_size
        self.assertGreater(residual, Decimal("0"))
        transport = InfoTransport(
            {
                CLOSE_CLOID: status_response(
                    cloid=CLOSE_CLOID,
                    oid=501,
                    status="canceled",
                    remaining=str(stop_size),
                    original="0.5",
                ),
                stop.cloid: status_response(
                    cloid=stop.cloid,
                    oid=601,
                    status="filled",
                    remaining="0",
                    original=str(stop_size),
                    trigger=True,
                    reduce_only=True,
                ),
            },
            [[
                raw_fill(
                    oid=601,
                    tid=10,
                    size=str(stop_size),
                    start_position="0.5",
                    time_ms=SERVER_MS - 100,
                ),
                raw_fill(
                    oid=501,
                    tid=11,
                    size=str(residual),
                    start_position=str(residual),
                    time_ms=SERVER_MS - 50,
                ),
            ]],
        )

        result = self._read(command, account_snapshot(), transport)

        self.assertTrue(result.fill_chain_complete)
        self.assertEqual(1, len(result.signed_fills))
        self.assertEqual(CLOSE_CLOID, result.signed_fills[0].cloid)
        self.assertEqual(1, len(result.auxiliary_fills))
        self.assertEqual("parent_leg", result.auxiliary_fills[0].owner_kind)
        self.assertEqual(stop.cloid, result.auxiliary_fills[0].fill.cloid)
        combined = sorted(
            [result.auxiliary_fills[0].fill, result.signed_fills[0]],
            key=lambda item: item.time_ms,
        )
        self.assertEqual(combined[0].end_position, combined[1].start_position)
        self.assertEqual(Decimal("0"), combined[-1].end_position)

    def test_cancel_request_can_be_missing_or_definitively_canceled(self) -> None:
        for response, expected_state in (
            ({"status": "unknownOid"}, VenueOrderState.MISSING),
            (None, VenueOrderState.ORDER),
        ):
            with self.subTest(expected_state=expected_state):
                self.tearDown()
                self.setUp()
                command, cloid = self._cancel_command()
                selected = (
                    status_response(
                        cloid=cloid,
                        oid=601,
                        status="canceled",
                        remaining="0",
                        trigger=True,
                    )
                    if response is None
                    else response
                )
                result = self._read(
                    command,
                    account_snapshot(signed_position="0.5", orders=[raw_order()]),
                    InfoTransport({cloid: selected}, [[]]),
                )
                self.assertEqual(result.order_statuses[0].state, expected_state)
                if expected_state is VenueOrderState.ORDER:
                    self.assertEqual(
                        result.order_statuses[0].venue_status, "canceled"
                    )

    def test_cancel_batch_preserves_exact_request_count_and_roles(self) -> None:
        incident = self._incident()
        second_cloid = "0x" + "e" * 32
        open_orders = [
            *valid_orders(),
            raw_order(
                oid=103,
                cloid=second_cloid,
                order_type="Take Market",
                trigger_price="3200",
                trigger_condition="Triggered above 3200",
            ),
        ]
        initial = account_snapshot(
            at=NOW + timedelta(seconds=6),
            signed_position="0.5",
            orders=open_orders,
        )
        first_cloid = open_orders[1]["cloid"]  # type: ignore[index]
        action = build_cancel_by_cloid(
            initial,
            (
                CancelRequest("ETH", first_cloid),
                CancelRequest("ETH", second_cloid),
            ),
            owned_cloids=tuple(
                order["cloid"] for order in open_orders  # type: ignore[index]
            ),
            incident=incident,
            account_id=self.store.account_id,
            network=HyperliquidNetwork.TESTNET,
            at=NOW + timedelta(seconds=6),
        )
        command = self._queue(action)
        statuses = {
            first_cloid: {"status": "unknownOid"},
            second_cloid: status_response(
                cloid=second_cloid,
                oid=602,
                status="canceled",
                remaining="0",
                trigger=True,
            ),
        }
        transport = InfoTransport(statuses, [[]])

        result = self._read(
            command,
            account_snapshot(signed_position="0.5", orders=[raw_order()]),
            transport,
        )

        self.assertEqual(
            tuple(item.role for item in result.order_statuses),
            ("cancel_request_0", "cancel_request_1"),
        )
        self.assertEqual(
            tuple(call[1]["oid"] for call in transport.calls[:2]),
            (first_cloid, second_cloid),
        )
        self.assertEqual(len(result.order_statuses), 2)

    def test_noop_loads_and_queries_exact_parent_three_legs_from_store(self) -> None:
        command = self._noop_command()
        parent_legs = self.store.get_legs("command-1")
        transport = InfoTransport(
            {leg.cloid: {"status": "unknownOid"} for leg in parent_legs},
            [[]],
        )

        result = self._read(command, account_snapshot(), transport)

        self.assertEqual(
            tuple(item.role for item in result.order_statuses),
            ("entry", "protective_stop", "take_profit"),
        )
        self.assertEqual(
            tuple(call[1]["oid"] for call in transport.calls[:3]),
            tuple(leg.cloid for leg in parent_legs),
        )
        self.assertEqual(transport.calls[-1][1]["type"], "userFillsByTime")
        self.assertTrue(result.fill_chain_complete)

    def test_fill_later_than_snapshot_is_rejected(self) -> None:
        command = self._close_command()
        transport = InfoTransport(
            {
                CLOSE_CLOID: status_response(
                    cloid=CLOSE_CLOID,
                    oid=501,
                    status="filled",
                    remaining="0",
                )
            },
            [[
                raw_fill(
                    oid=501,
                    tid=1,
                    size="0.5",
                    start_position="0.5",
                    time_ms=SERVER_MS + 1,
                )
            ]],
        )
        with self.assertRaises(HyperliquidReconcileResponseError):
            self._read(command, account_snapshot(), transport)

    def test_stale_snapshot_fails_before_any_info_request(self) -> None:
        command = self._close_command()
        transport = InfoTransport({}, [])
        stale = account_snapshot(at=READ_AT - timedelta(seconds=6))

        with self.assertRaisesRegex(ValidationError, "stale"):
            self._read(command, stale, transport)

        self.assertEqual(transport.calls, [])

    def test_foreign_cloid_and_foreign_fill_oid_fail_closed(self) -> None:
        command = self._close_command()
        foreign_status = status_response(
            cloid="0x" + "f" * 32,
            oid=501,
            status="filled",
            remaining="0",
        )
        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "foreign CLOID"
        ):
            self._read(
                command,
                account_snapshot(),
                InfoTransport({CLOSE_CLOID: foreign_status}, [[]]),
            )

        own_status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="open",
            remaining="0.5",
        )
        result = self._read(
            command,
            account_snapshot(signed_position="0.5"),
            InfoTransport(
                {CLOSE_CLOID: own_status},
                [[
                    raw_fill(
                        oid=999,
                        tid=2,
                        size="0.1",
                        start_position="0",
                        time_ms=SERVER_MS - 50,
                        side="B",
                    )
                ]],
            ),
        )
        self.assertEqual(result.signed_fills, ())
        self.assertEqual(result.fill_coverage.unmatched_fills, 1)
        self.assertFalse(result.fill_coverage.complete)
        self.assertFalse(result.fill_chain_complete)

    def test_full_page_uses_inclusive_boundary_and_deduplicates(self) -> None:
        command = self._close_command()
        status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="filled",
            remaining="0",
        )
        first = raw_fill(
            oid=501,
            tid=10,
            size="0.1",
            start_position="0.5",
            time_ms=START_MS + 10,
        )
        boundary = raw_fill(
            oid=501,
            tid=11,
            size="0.2",
            start_position="0.4",
            time_ms=START_MS + 20,
        )
        last = raw_fill(
            oid=501,
            tid=12,
            size="0.2",
            start_position="0.2",
            time_ms=START_MS + 30,
        )
        transport = InfoTransport(
            {CLOSE_CLOID: status},
            [[first, boundary], [deepcopy(boundary), last], [deepcopy(last)]],
        )
        with mock.patch.object(reader_module, "USER_FILLS_PAGE_LIMIT", 2):
            result = self._read(command, account_snapshot(), transport)

        fill_calls = [
            payload
            for _, payload in transport.calls
            if payload["type"] == "userFillsByTime"
        ]
        self.assertEqual(
            tuple(item["startTime"] for item in fill_calls),
            (START_MS, START_MS + 20, START_MS + 30),
        )
        self.assertEqual(result.fill_coverage.duplicate_fills, 2)
        self.assertEqual(result.fill_coverage.unique_fills, 3)
        self.assertFalse(result.fill_coverage.page_saturated)
        self.assertTrue(result.fill_chain_complete)

    def test_inclusive_boundary_saturation_is_explicitly_incomplete(self) -> None:
        command = self._close_command()
        status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="open",
            remaining="0.5",
        )
        first = raw_fill(
            oid=501,
            tid=10,
            size="0.1",
            start_position="0.5",
            time_ms=START_MS + 10,
        )
        boundary = raw_fill(
            oid=501,
            tid=11,
            size="0.1",
            start_position="0.4",
            time_ms=START_MS + 20,
        )
        same_boundary = raw_fill(
            oid=501,
            tid=12,
            size="0.1",
            start_position="0.3",
            time_ms=START_MS + 20,
        )
        transport = InfoTransport(
            {CLOSE_CLOID: status},
            [[first, boundary], [deepcopy(boundary), same_boundary]],
        )
        with mock.patch.object(reader_module, "USER_FILLS_PAGE_LIMIT", 2):
            result = self._read(
                command, account_snapshot(signed_position="0.2"), transport
            )

        self.assertTrue(result.fill_coverage.page_saturated)
        self.assertEqual(
            result.fill_coverage.reason, "inclusive_page_boundary_saturated"
        )
        self.assertFalse(result.fill_chain_complete)

    def test_inclusive_page_requires_boundary_overlap(self) -> None:
        command = self._close_command()
        status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="filled",
            remaining="0",
        )
        first = raw_fill(
            oid=501,
            tid=20,
            size="0.2",
            start_position="0.5",
            time_ms=START_MS + 10,
        )
        boundary = raw_fill(
            oid=501,
            tid=21,
            size="0.1",
            start_position="0.3",
            time_ms=START_MS + 20,
        )
        later = raw_fill(
            oid=501,
            tid=22,
            size="0.2",
            start_position="0.2",
            time_ms=START_MS + 30,
        )
        with mock.patch.object(reader_module, "USER_FILLS_PAGE_LIMIT", 2):
            with self.assertRaisesRegex(
                HyperliquidReconcileResponseError, "overlap"
            ):
                self._read(
                    command,
                    account_snapshot(),
                    InfoTransport(
                        {CLOSE_CLOID: status},
                        [[first, boundary], [later]],
                    ),
                )

    def test_recovery_fill_page_must_be_ascending(self) -> None:
        command = self._close_command()
        status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="open",
            remaining="0.5",
        )
        later = raw_fill(
            oid=501,
            tid=31,
            size="0.1",
            start_position="0.5",
            time_ms=START_MS + 20,
        )
        earlier = raw_fill(
            oid=501,
            tid=30,
            size="0.1",
            start_position="0.4",
            time_ms=START_MS + 10,
        )
        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "ascending"
        ):
            self._read(
                command,
                account_snapshot(signed_position="0.3"),
                InfoTransport({CLOSE_CLOID: status}, [[later, earlier]]),
            )

    def test_retention_limit_is_explicitly_incomplete(self) -> None:
        command = self._close_command()
        status = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="open",
            remaining="0.5",
        )
        pages = [
            [
                raw_fill(
                    oid=501,
                    tid=index + 1,
                    size="0.01",
                    start_position=str(Decimal("0.5") - Decimal("0.01") * index),
                    time_ms=START_MS + index + 1,
                )
                for index in range(2)
            ],
            [
                deepcopy(
                    raw_fill(
                        oid=501,
                        tid=2,
                        size="0.01",
                        start_position="0.49",
                        time_ms=START_MS + 2,
                    )
                ),
                raw_fill(
                    oid=501,
                    tid=3,
                    size="0.01",
                    start_position="0.48",
                    time_ms=START_MS + 3,
                ),
            ],
            [
                deepcopy(
                    raw_fill(
                        oid=501,
                        tid=3,
                        size="0.01",
                        start_position="0.48",
                        time_ms=START_MS + 3,
                    )
                ),
                raw_fill(
                    oid=501,
                    tid=4,
                    size="0.01",
                    start_position="0.47",
                    time_ms=START_MS + 4,
                ),
            ],
        ]
        transport = InfoTransport({CLOSE_CLOID: status}, pages)
        with (
            mock.patch.object(reader_module, "USER_FILLS_PAGE_LIMIT", 2),
            mock.patch.object(reader_module, "USER_FILLS_RETENTION_LIMIT", 4),
        ):
            result = self._read(
                command, account_snapshot(signed_position="0.46"), transport
            )

        self.assertTrue(result.fill_coverage.retention_limited)
        self.assertFalse(result.fill_coverage.complete)
        self.assertEqual(
            result.fill_coverage.reason, "latest_10000_fill_retention_limit"
        )

    def test_unknown_schema_and_duplicate_status_oid_are_rejected(self) -> None:
        command = self._close_command()
        unsupported = status_response(
            cloid=CLOSE_CLOID,
            oid=501,
            status="open",
            remaining="0.5",
        )
        unsupported["order"]["order"]["newField"] = "surprise"  # type: ignore[index]
        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "fields are unsupported"
        ):
            self._read(
                command,
                account_snapshot(signed_position="0.5"),
                InfoTransport({CLOSE_CLOID: unsupported}, [[]]),
            )

        self.tearDown()
        self.setUp()
        noop = self._noop_command()
        parent = self.store.get_legs("command-1")
        statuses = {
            leg.cloid: status_response(
                cloid=leg.cloid,
                oid=777,
                status="open",
                remaining=str(leg.requested_quantity),
                original=str(leg.requested_quantity),
                side="B" if leg.side == "buy" else "A",
                trigger=leg.role != "entry",
                reduce_only=leg.role != "entry",
            )
            for leg in parent
        }
        with self.assertRaisesRegex(
            HyperliquidReconcileResponseError, "repeats a venue OID"
        ):
            self._read(noop, account_snapshot(), InfoTransport(statuses, [[]]))

    def test_command_must_equal_durable_recovery_record(self) -> None:
        command = self._close_command()
        forged = replace(command, recovery_material_hash=hashlib.sha256(b"x").hexdigest())
        transport = InfoTransport({}, [])

        with self.assertRaisesRegex(StateConflict, "durable state"):
            self._read(forged, account_snapshot(), transport)

        self.assertEqual(transport.calls, [])

    def test_reader_is_observational_and_calls_only_allowlisted_info_reads(self) -> None:
        command = self._close_command()
        transport = InfoTransport(
            {
                CLOSE_CLOID: status_response(
                    cloid=CLOSE_CLOID,
                    oid=501,
                    status="open",
                    remaining="0.5",
                )
            },
            [[]],
        )
        events_before = self.store.list_events()
        command_before = self.store.get_recovery_command(
            command.recovery_command_id
        )

        self._read(
            command,
            account_snapshot(signed_position="0.5"),
            transport,
        )

        self.assertEqual(self.store.list_events(), events_before)
        self.assertEqual(
            self.store.get_recovery_command(command.recovery_command_id),
            command_before,
        )
        self.assertEqual(
            tuple(endpoint for endpoint, _ in transport.calls),
            (TESTNET_INFO,) * 5,
        )
        self.assertEqual(
            tuple(payload["type"] for _, payload in transport.calls),
            (
                "orderStatus",
                "orderStatus",
                "orderStatus",
                "orderStatus",
                "userFillsByTime",
            ),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
