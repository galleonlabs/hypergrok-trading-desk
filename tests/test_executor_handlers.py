from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from trading_harness.account_safety_controller import (
    SafetyControllerState,
    TestnetAccountSafetyController,
)
from trading_harness.approval import TestnetRecoveryAuthority
from trading_harness.errors import StateConflict
from trading_harness.execution_store import (
    AttemptRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryAttempt,
    RecoveryCommand,
    RecoveryOutbox,
    RecoveryReconciliationProof,
)
from trading_harness.executor_handlers import build_testnet_executor_handlers
from trading_harness.executor_runtime import HandlerDisposition
from trading_harness.hyperliquid_reconcile import FillCoverage
from trading_harness.hyperliquid_recovery import RecoveryKind
from trading_harness.hyperliquid_recovery_reader import (
    HyperliquidRecoveryVenueReader,
)
from trading_harness.hyperliquid_signer import SignerPolicy, SigningAccount
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.reconciliation_coordinator import (
    HyperliquidVenueReconciler,
    MainEntryReconciliationCoordinator,
    MainReconciliationResult,
)
from trading_harness.recovery_reconciliation import (
    RecoveryCoordinationResult,
    RecoveryReconciliationCoordinator,
    RecoveryVenueRead,
)
from tests.test_account_safety_controller import RECOVERY_CLOID, market_brief
from tests.test_execution_store import ExecutionStoreTestCase, NOW, digest
from tests.test_hyperliquid_account import (
    ACCOUNT,
    FixtureTransport,
    fetch as fetch_account,
    raw_position,
    valid_clearing,
)
from tests.test_hyperliquid_signer import SIGNER


AT = NOW + timedelta(seconds=6)


def milliseconds(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


class ExecutorHandlerTests(ExecutionStoreTestCase):
    def snapshot(self, *, size: str | None = None):
        positions = [] if size is None else [raw_position(signed_size=size)]
        clearing = valid_clearing(
            positions=positions,
            server_time=milliseconds(AT) - 500,
        )
        if size is None:
            for field in ("marginSummary", "crossMarginSummary"):
                clearing[field]["totalNtlPos"] = "0"  # type: ignore[index]
                clearing[field]["totalMarginUsed"] = "0"  # type: ignore[index]
        value, _ = fetch_account(
            FixtureTransport(
                clearing=clearing,
                orders=[],
            ),
            received_at_ms=milliseconds(AT),
            network="testnet",
        )
        return value

    def controller(self) -> TestnetAccountSafetyController:
        owned = {RECOVERY_CLOID}
        for command in self.store.list_commands():
            owned.update(item.cloid for item in self.store.get_legs(command.command_id))
        policy = SignerPolicy(
            accounts=(
                SigningAccount(
                    account_id=self.store.account_id,
                    main_account_address=ACCOUNT,
                    signer_address=SIGNER,
                    owned_cloids=frozenset(owned),
                ),
            ),
            allowed_asset_ids=frozenset({1}),
            allowed_networks=frozenset({HyperliquidNetwork.TESTNET}),
            allowed_recovery_kinds=frozenset(
                {
                    RecoveryKind.REDUCE_ONLY_CLOSE,
                    RecoveryKind.CANCEL_BY_CLOID,
                    RecoveryKind.NOOP_FENCE,
                }
            ),
        )
        return TestnetAccountSafetyController(
            self.store,
            signer_policy=policy,
            recovery_authority=TestnetRecoveryAuthority(
                b"h" * 32,
                key_id="handler-safety-v1",
                issuer_id="isolated-handler-safety",
                audience="testnet-recovery-worker",
            ),
        )

    def handlers(
        self,
        account_reader,
        *,
        market_reader=None,
        worker_id: str = "runtime-reconciler",
    ):
        controller = self.controller()
        return build_testnet_executor_handlers(
            store=self.store,
            account_reader=account_reader,
            main_coordinator=MainEntryReconciliationCoordinator(
                self.store,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: AT,
            ),
            venue_reconciler=HyperliquidVenueReconciler(
                transport=lambda *_args: {},
                clock=lambda: AT,
            ),
            recovery_coordinator=RecoveryReconciliationCoordinator(self.store),
            recovery_venue_reader=HyperliquidRecoveryVenueReader(
                self.store,
                transport=lambda *_args: {},
                clock=lambda: AT,
            ),
            safety_controller=controller,
            worker_id=worker_id,
            market_brief_reader=market_reader,
            clock=lambda: AT,
        )

    def test_startup_reads_one_complete_snapshot_and_only_safe_becomes_complete(self) -> None:
        snapshot = self.snapshot(size=None)
        calls: list[tuple[str, str]] = []

        handlers = self.handlers(
            lambda address, network: calls.append((address, network)) or snapshot
        )
        result = handlers.startup_reconciler.reconcile_startup()

        self.assertIs(result.disposition, HandlerDisposition.COMPLETE)
        self.assertFalse(result.local_state_changed)
        self.assertEqual(calls, [(ACCOUNT, "testnet")])
        self.assertEqual(handlers.runtime_ports()["safety_handler"], handlers.safety_handler)

    def test_startup_refuses_pending_reconciliation_without_account_read(self) -> None:
        self.prepare_unknown()
        calls: list[object] = []
        handlers = self.handlers(
            lambda *_args: calls.append(_args),
            worker_id="reconciler",
        )

        result = handlers.startup_reconciler.reconcile_startup()

        self.assertIs(result.disposition, HandlerDisposition.WAITING)
        self.assertFalse(result.local_state_changed)
        self.assertEqual(calls, [])

    def test_safety_caches_exact_one_shot_preparation_for_dispatcher(self) -> None:
        self.admit_one()
        self.store.record_incident(
            incident_id="handler-under-protected",
            command_id="command-1",
            code="POSITION_UNDER_PROTECTED",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )
        snapshot = self.snapshot(size="0.2")
        handlers = self.handlers(
            lambda *_args: snapshot,
            market_reader=lambda *_args: market_brief(AT),
        )

        queued = handlers.safety_handler.act_next()
        active = handlers.safety_handler.act_next()
        claim = self.store.claim_next_recovery(
            "dispatcher",
            at=AT,
            lease_seconds=30,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        command = self.store.get_recovery_command(claim.recovery_command_id)
        prepared = handlers.safety_handler.prepare(command, at=AT)

        self.assertIs(queued.disposition, HandlerDisposition.PROGRESSED)
        self.assertTrue(queued.local_state_changed)
        self.assertIs(active.disposition, HandlerDisposition.NO_WORK)
        self.assertFalse(active.local_state_changed)
        self.assertEqual(prepared.action.recovery_hash, command.recovery_hash)
        self.assertIs(prepared.evidence, snapshot)
        with self.assertRaisesRegex(StateConflict, "no exact"):
            handlers.safety_handler.prepare(command, at=AT)

    def test_parent_handler_selects_one_exact_record_and_applies_one_transition(self) -> None:
        ticket, _ = self.prepare_unknown()
        snapshot = self.snapshot(size=None)
        handlers = self.handlers(lambda *_args: snapshot, worker_id="reconciler")
        calls: list[dict[str, object]] = []

        bundle = object()

        def read(_coordinator, _venue, selected_snapshot, **kwargs):
            calls.append({"phase": "read", "snapshot": selected_snapshot, **kwargs})
            return bundle

        def apply(_coordinator, selected_bundle, selected_snapshot, **kwargs):
            self.assertIs(selected_bundle, bundle)
            calls.append({"phase": "apply", "snapshot": selected_snapshot, **kwargs})
            self.store.record_incident(
                incident_id="parent-handler-transition",
                command_id="command-1",
                code="HANDLER_TEST_TRANSITION",
                severity="warning",
                at=AT,
            )
            reserved = self.store.get_reserved_exposure()
            return MainReconciliationResult(
                command_id="command-1",
                reconciliation_hash=digest("handler-main-reconcile"),
                command_state="reconciling",
                evidence_complete=False,
                terminal=False,
                protection_state="flat",
                signed_position_quantity=Decimal("0"),
                protected_quantity=Decimal("0"),
                risk_released_loss=Decimal("0"),
                risk_released_notional=Decimal("0"),
                residual_command_reserved_loss=ticket.stressed_loss,
                residual_command_reserved_notional=self.store.get_command(
                    "command-1"
                ).reserved_notional,
                account_reserved_loss=reserved[0],
                account_reserved_notional=reserved[1],
                active_incident_ids=("parent-handler-transition",),
            )

        with (
            patch.object(
                MainEntryReconciliationCoordinator,
                "read_bundle",
                autospec=True,
                side_effect=read,
            ),
            patch.object(
                MainEntryReconciliationCoordinator,
                "apply_bundle",
                autospec=True,
                side_effect=apply,
            ),
        ):
            result = handlers.parent_reconciler.reconcile_next()

        self.assertIs(result.disposition, HandlerDisposition.PROGRESSED)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["phase"], "read")
        self.assertEqual(calls[1]["phase"], "apply")
        self.assertIs(calls[0]["snapshot"], snapshot)
        self.assertEqual(calls[0]["command_id"], "command-1")
        self.assertEqual(calls[0]["fills_end_time_ms"], snapshot.server_time_ms)

    def test_parent_handler_real_coordinators_terminalize_flat_canceled_command(self) -> None:
        self.prepare_unknown()
        snapshot = self.snapshot(size=None)
        self.assertLess(snapshot.server_time_ms, milliseconds(AT))
        legs = self.store.get_legs("command-1")
        statuses: dict[str, object] = {}
        for index, leg in enumerate(legs):
            trigger = leg.role != "entry"
            statuses[leg.cloid] = {
                "status": "order",
                "order": {
                    "order": {
                        "coin": "ETH",
                        "side": "B" if leg.side == "buy" else "A",
                        "limitPx": "2500",
                        "sz": "0",
                        "oid": 500 + index,
                        "timestamp": snapshot.server_time_ms - 1_000,
                        "triggerCondition": "venue trigger" if trigger else "N/A",
                        "isTrigger": trigger,
                        "triggerPx": "2400" if trigger else "0",
                        "children": [],
                        "isPositionTpsl": False,
                        "reduceOnly": leg.reduce_only,
                        "orderType": (
                            "Stop Market"
                            if leg.role == "protective_stop"
                            else "Take Market"
                            if leg.role == "take_profit"
                            else "Market"
                        ),
                        "origSz": str(leg.requested_quantity),
                        "tif": "FrontendMarket" if trigger else "Ioc",
                        "cloid": leg.cloid,
                    },
                    "status": "canceled",
                    "statusTimestamp": snapshot.server_time_ms - 100,
                },
            }

        class ReadTransport:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __call__(
                self,
                _endpoint: str,
                payload: Mapping[str, object],
            ) -> object:
                self.calls.append(str(payload.get("type")))
                if payload.get("type") == "orderStatus":
                    return deepcopy(statuses[str(payload["oid"])])
                if payload.get("type") == "userFillsByTime":
                    return []
                raise AssertionError(f"unexpected read: {payload!r}")

        transport = ReadTransport()
        controller = self.controller()
        handlers = build_testnet_executor_handlers(
            store=self.store,
            account_reader=lambda *_args: snapshot,
            main_coordinator=MainEntryReconciliationCoordinator(
                self.store,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: AT,
            ),
            venue_reconciler=HyperliquidVenueReconciler(
                transport=transport,
                clock=lambda: AT,
            ),
            recovery_coordinator=RecoveryReconciliationCoordinator(self.store),
            recovery_venue_reader=HyperliquidRecoveryVenueReader(
                self.store,
                transport=lambda *_args: {},
                clock=lambda: AT,
            ),
            safety_controller=controller,
            worker_id="reconciler",
            clock=lambda: AT,
        )

        result = handlers.parent_reconciler.reconcile_next()

        self.assertIs(result.disposition, HandlerDisposition.PROGRESSED)
        self.assertEqual(self.store.get_command("command-1").state, "terminal")
        self.assertEqual(
            transport.calls,
            ["orderStatus", "orderStatus", "orderStatus", "userFillsByTime"],
        )

    def test_all_missing_unknown_parent_opens_exact_ambiguity_then_queues_noop(self) -> None:
        self.prepare_unknown()
        snapshot = self.snapshot(size=None)
        self.assertLess(snapshot.server_time_ms, milliseconds(AT))

        def transport(_endpoint: str, payload: Mapping[str, object]) -> object:
            if payload.get("type") == "orderStatus":
                return {"status": "unknownOid"}
            if payload.get("type") == "userFillsByTime":
                return []
            raise AssertionError(payload)

        controller = self.controller()
        handlers = build_testnet_executor_handlers(
            store=self.store,
            account_reader=lambda *_args: snapshot,
            main_coordinator=MainEntryReconciliationCoordinator(
                self.store,
                network=HyperliquidNetwork.TESTNET,
                clock=lambda: AT,
            ),
            venue_reconciler=HyperliquidVenueReconciler(
                transport=transport,
                clock=lambda: AT,
            ),
            recovery_coordinator=RecoveryReconciliationCoordinator(self.store),
            recovery_venue_reader=HyperliquidRecoveryVenueReader(
                self.store,
                transport=transport,
                clock=lambda: AT,
            ),
            safety_controller=controller,
            worker_id="reconciler",
            clock=lambda: AT,
        )

        parent = handlers.parent_reconciler.reconcile_next()
        incidents = self.store.list_incidents("command-1")
        retry = handlers.parent_reconciler.reconcile_next()
        released = self.store.get_outbox("command-1")
        safety = handlers.safety_handler.act_next()

        self.assertIs(parent.disposition, HandlerDisposition.PROGRESSED)
        self.assertIs(retry.disposition, HandlerDisposition.PROGRESSED)
        self.assertIsNone(released.worker_id)
        self.assertIsNone(released.lease_expires_at)
        self.assertIn(
            "UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING",
            {item.code for item in incidents},
        )
        self.assertIs(safety.disposition, HandlerDisposition.PROGRESSED)
        recovery = self.store.list_recovery_commands(active_only=True)
        self.assertEqual(1, len(recovery))
        self.assertEqual("noop_fence", recovery[0].kind)

    def test_recovery_handler_selects_highest_priority_exact_record_once(self) -> None:
        snapshot = self.snapshot(size=None)
        handlers = self.handlers(lambda *_args: snapshot)
        command = RecoveryCommand(
            recovery_command_id="recovery-1",
            permit_id="permit-1",
            parent_command_id="parent-1",
            incident_id="incident-1",
            kind="cancel_by_cloid",
            priority=1,
            source_hash=digest("source"),
            preflight_hash=None,
            recovery_hash=digest("recovery"),
            recovery_material_json=(
                '{"account_snapshot_time_ms":'
                f"{milliseconds(NOW)}"
                "}"
            ),
            recovery_material_hash=digest("material"),
            safety_policy_hash=digest("policy"),
            original_attempt_id=None,
            original_nonce=None,
            state="reconciling",
            created_at=NOW,
            updated_at=NOW,
            terminal_at=None,
            revision=1,
        )
        outbox = RecoveryOutbox(
            recovery_command_id="recovery-1",
            state="reconciling",
            worker_id=None,
            fencing_token=1,
            claimed_at=None,
            lease_expires_at=None,
            current_attempt_id="recovery-attempt-1",
            attempt_count=1,
            created_at=NOW,
            updated_at=NOW,
        )
        attempt = RecoveryAttempt(
            attempt_id="recovery-attempt-1",
            recovery_command_id="recovery-1",
            worker_id="dispatcher",
            fencing_token=1,
            signed_evidence_hash=digest("signed"),
            transport_evidence_hash=digest("transport"),
            nonce=1,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            state="response_received",
            prepared_at=NOW,
            updated_at=NOW,
        )
        coverage = FillCoverage(
            requested_start_time_ms=milliseconds(NOW),
            requested_end_time_ms=snapshot.server_time_ms,
            page_count=1,
            page_limit=2_000,
            retention_limit=10_000,
            returned_rows=0,
            unique_fills=0,
            duplicate_fills=0,
            unmatched_fills=0,
            page_saturated=False,
            retention_limited=False,
            complete=True,
            reason="range_exhausted",
        )
        evidence = RecoveryVenueRead(
            network="testnet",
            account_id=self.store.account_id,
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=AT,
            order_statuses=(),
            signed_fills=(),
            fill_coverage=coverage,
        )
        proof = RecoveryReconciliationProof(
            recovery_command_id="recovery-1",
            kind="cancel_by_cloid",
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=AT,
            signed_position_quantity=Decimal("0"),
            protected_quantity=Decimal("0"),
            open_order_cloids=(),
            affected_cloids=(),
            resolved_original_nonce=None,
            resolved_original_outcome=None,
            complete=False,
            success=False,
        )
        calls: list[str] = []

        def reconcile(_coordinator, recovery_id, _worker, **_kwargs):
            calls.append(recovery_id)
            self.store.record_incident(
                incident_id="recovery-handler-transition",
                command_id=None,
                code="HANDLER_TEST_TRANSITION",
                severity="warning",
                at=AT,
            )
            return RecoveryCoordinationResult(
                recovery_command_id=recovery_id,
                recovery_state="reconciling",
                proof=proof,
                incomplete_reasons=("test",),
                incident_resolution=None,
            )

        with (
            patch.object(self.store, "list_recovery_commands", return_value=(command,)),
            patch.object(self.store, "list_recovery_outboxes", return_value=(outbox,)),
            patch.object(self.store, "get_recovery_attempt", return_value=attempt),
            patch.object(
                HyperliquidRecoveryVenueReader,
                "read",
                autospec=True,
                return_value=evidence,
            ),
            patch.object(
                RecoveryReconciliationCoordinator,
                "reconcile",
                autospec=True,
                side_effect=reconcile,
            ),
        ):
            result = handlers.recovery_reconciler.reconcile_next()

        self.assertIs(result.disposition, HandlerDisposition.PROGRESSED)
        self.assertEqual(calls, ["recovery-1"])

    def test_protection_inspection_opens_one_stable_critical_incident(self) -> None:
        self.admit_one()
        snapshot = self.snapshot(size="0.2")
        stop = next(
            item for item in self.store.get_legs("command-1") if item.role == "protective_stop"
        )
        position = PositionRecord(
            instrument="ETH-PERP",
            signed_quantity=Decimal("0.2"),
            account_snapshot_hash=digest("prior-account"),
            observed_at=NOW,
            revision=1,
        )
        protection = ProtectionRecord(
            command_id="command-1",
            instrument="ETH-PERP",
            state="protected",
            signed_position_quantity=Decimal("0.2"),
            protected_quantity=Decimal("0.2"),
            stop_cloid=stop.cloid,
            observed_at=NOW,
            revision=1,
        )
        handlers = self.handlers(lambda *_args: snapshot)

        with (
            patch.object(self.store, "list_positions", return_value=(position,)),
            patch.object(self.store, "list_protections", return_value=(protection,)),
        ):
            first = handlers.protection_inspector.inspect_next()
            second = handlers.protection_inspector.inspect_next()

        incidents = self.store.list_incidents("command-1")
        self.assertIs(first.disposition, HandlerDisposition.PROGRESSED)
        self.assertIs(second.disposition, HandlerDisposition.NO_WORK)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].code, "PROTECTION_FAILED")
        self.assertEqual(incidents[0].severity, "critical")


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
