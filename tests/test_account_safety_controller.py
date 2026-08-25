from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
import unittest
from unittest.mock import patch

from trading_harness.account_safety_controller import (
    SafetyControllerPolicy,
    SafetyControllerState,
    TestnetAccountSafetyController,
)
from trading_harness.approval import TestnetRecoveryAuthority
from trading_harness.errors import RecordNotFound, ValidationError
from trading_harness.hyperliquid_recovery import (
    CancelByCloidAction,
    NoopFenceAction,
    RecoveryKind,
    ReduceOnlyCloseAction,
    derive_recovery_close_cloid,
)
from trading_harness.hyperliquid_signer import SignerPolicy, SigningAccount
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from tests.test_execution_store import ExecutionStoreTestCase, NOW
from tests.test_hyperliquid_account import (
    ACCOUNT,
    FixtureTransport,
    raw_order,
    raw_position,
    valid_clearing,
    fetch as fetch_account,
)
from tests.test_hyperliquid_signer import SIGNER


AT = NOW + timedelta(seconds=6)
RECOVERY_CLOID = "0x" + "e" * 32
FOREIGN_CLOID = "0x" + "f" * 32


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1_000)


def market_brief(
    at: datetime,
    *,
    symbol: str = "ETH",
    bid_depth: str = "10",
    ask_depth: str = "10",
    time_offset_ms: int = -100,
) -> dict[str, object]:
    observed_ms = _milliseconds(at) + time_offset_ms
    age = max(-time_offset_ms, 0)
    endpoint = "https://api.hyperliquid-testnet.xyz/info"
    return {
        "schema_version": "hyperliquid.market_brief.v1",
        "venue": "hyperliquid",
        "network": "testnet",
        "symbol": symbol,
        "observed_at": datetime.fromtimestamp(
            observed_ms / 1_000, tz=timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "received_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "age_ms": age,
        "sources": [
            {
                "url": endpoint,
                "endpoint": "/info",
                "request_type": "metaAndAssetCtxs",
            },
            {
                "url": endpoint,
                "endpoint": "/info",
                "request_type": "l2Book",
            },
        ],
        "mid_consistency": {"within_limit": True},
        "book": {
            "time_ms": observed_ms,
            "mid": "2500",
            "best_bid": "2499",
            "best_ask": "2501",
            "depth": {
                "5bps": {
                    "bid_size": bid_depth,
                    "ask_size": ask_depth,
                    "bid_complete": True,
                    "ask_complete": True,
                },
                "10bps": {
                    "bid_size": bid_depth,
                    "ask_size": ask_depth,
                    "bid_complete": True,
                    "ask_complete": True,
                },
                "25bps": {
                    "bid_size": bid_depth,
                    "ask_size": ask_depth,
                    "bid_complete": True,
                    "ask_complete": True,
                },
            },
        },
    }


class AccountSafetyControllerTests(ExecutionStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket, _ = self.admit_one()
        self.legs = self.store.get_legs("command-1")
        self.cloids = frozenset(item.cloid for item in self.legs)

    def _snapshot(
        self,
        *,
        at: datetime = AT,
        size: str | None = "0.2",
        orders: list[object] | None = None,
        server_offset_ms: int = -500,
    ):
        server_ms = _milliseconds(at) + server_offset_ms
        positions = [] if size is None else [raw_position(signed_size=size)]
        clearing = valid_clearing(
            positions=positions,
            server_time=server_ms,
        )
        if size is None:
            for field in ("marginSummary", "crossMarginSummary"):
                clearing[field]["totalNtlPos"] = "0"  # type: ignore[index]
                clearing[field]["totalMarginUsed"] = "0"  # type: ignore[index]
        result, _ = fetch_account(
            FixtureTransport(
                clearing=clearing,
                orders=[] if orders is None else orders,
            ),
            received_at_ms=_milliseconds(at),
            network="testnet",
        )
        return result

    def _controller(self) -> TestnetAccountSafetyController:
        signer_policy = SignerPolicy(
            accounts=(
                SigningAccount(
                    account_id="testnet-account",
                    main_account_address=ACCOUNT,
                    signer_address=SIGNER,
                    # Static signer policy owns only reserved flatten IDs.
                    # Parent entry/stop/target IDs are trusted dynamically
                    # from the exact durable protected plan.
                    owned_cloids=frozenset({RECOVERY_CLOID}),
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
            signer_policy=signer_policy,
            recovery_authority=TestnetRecoveryAuthority(
                b"s" * 32,
                key_id="account-safety-v1",
                issuer_id="isolated-account-safety-controller",
                audience="testnet-recovery-worker",
            ),
        )

    def _incident(
        self,
        *,
        code: str = "POSITION_UNDER_PROTECTED",
        incident_id: str = "incident-under-protected",
        at: datetime = NOW + timedelta(seconds=5),
    ):
        return self.store.record_incident(
            incident_id=incident_id,
            command_id="command-1",
            code=code,
            severity="critical",
            at=at,
            details={"source": "verified-reconciliation"},
        )

    def _terminal_prepared_noop_fixture(self):
        # Start from the durable unknown-parent state used by noop recovery.
        self.tearDown()
        ExecutionStoreTestCase.setUp(self)
        self.ticket, _ = self.prepare_unknown()
        self.legs = self.store.get_legs("command-1")
        self.cloids = frozenset(item.cloid for item in self.legs)
        self._incident(
            code="UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING",
            incident_id="incident-unknown-parent",
        )
        controller = self._controller()
        initial_snapshot = self._snapshot(size=None, at=AT)
        first = controller.evaluate(initial_snapshot, None, at=AT)
        self.assertIs(first.state, SafetyControllerState.QUEUED)
        assert first.recovery_command is not None
        claim = self.store.claim_next_recovery(
            "recovery-worker",
            at=NOW + timedelta(seconds=7),
            lease_seconds=3,
        )
        assert claim is not None
        authority = self.store.require_recovery_signing_authority(
            first.recovery_command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=7, milliseconds=100),
        )
        parent_attempt = self.store.get_attempt("command-1")
        signed = self.make_signed_recovery(
            first.recovery_command,
            signing_authority_hash=authority.authority_hash,
            nonce=parent_attempt.nonce,
        )
        recovery_attempt = self.store.prepare_recovery_attempt(
            first.recovery_command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="noop-proven-unsent-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=8),
        )
        self.assertIsNone(
            self.store.claim_next_recovery(
                "expiry-normalizer",
                at=NOW + timedelta(seconds=10),
                lease_seconds=5,
            )
        )
        terminal = self.store.get_recovery_command(
            first.recovery_command.recovery_command_id
        )
        self.assertEqual(terminal.state, "terminal")
        self.assertEqual(recovery_attempt.state, "prepared")
        self.assertEqual(
            self.store.get_recovery_attempt(terminal.recovery_command_id).state,
            "prepared",
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_transport_evidence(
                terminal.recovery_command_id
            )
        return controller, terminal, recovery_attempt

    def test_under_protected_partial_long_queues_full_bounded_reduce_only_ioc(self) -> None:
        self._incident()
        snapshot = self._snapshot(size="0.2")

        result = self._controller().evaluate(
            snapshot,
            market_brief(AT),
            at=AT,
        )

        self.assertIs(result.state, SafetyControllerState.QUEUED)
        self.assertEqual(result.reason_code, "REDUCE_ONLY_CLOSE_QUEUED")
        self.assertIsNotNone(result.prepared)
        assert result.prepared is not None
        self.assertIs(result.prepared.evidence, snapshot)
        action = result.prepared.action
        self.assertIsInstance(action, ReduceOnlyCloseAction)
        assert isinstance(action, ReduceOnlyCloseAction)
        self.assertEqual(action.original_signed_position, Decimal("0.2"))
        self.assertEqual(action.close_size, Decimal("0.2"))
        self.assertEqual(action.price_bound, Decimal("2492.8"))
        self.assertEqual(
            action.cloid,
            derive_recovery_close_cloid(
                account_id="testnet-account",
                incident_id="incident-under-protected",
                position_snapshot_hash=snapshot.snapshot_hash,
            ),
        )
        self.assertNotIn(action.cloid, self._controller().signing_account.owned_cloids)
        order = action.action["orders"][0]  # type: ignore[index]
        self.assertFalse(order["b"])
        self.assertTrue(order["r"])
        self.assertEqual(order["t"], {"limit": {"tif": "Ioc"}})
        self.assertEqual(len(self.store.list_recovery_commands(active_only=True)), 1)

    def test_partial_short_uses_fresh_actual_size_and_adverse_buy_bound(self) -> None:
        self._incident(code="POSITION_DIRECTION_CONTRADICTION")
        snapshot = self._snapshot(size="-0.075")

        result = self._controller().evaluate(snapshot, market_brief(AT), at=AT)

        self.assertIs(result.state, SafetyControllerState.QUEUED)
        assert result.prepared is not None
        action = result.prepared.action
        self.assertIsInstance(action, ReduceOnlyCloseAction)
        assert isinstance(action, ReduceOnlyCloseAction)
        self.assertEqual(action.original_signed_position, Decimal("-0.075"))
        self.assertEqual(action.close_size, Decimal("0.075"))
        self.assertEqual(action.price_bound, Decimal("2507.2"))
        self.assertTrue(action.action["orders"][0]["b"])  # type: ignore[index]
        self.assertTrue(action.action["orders"][0]["r"])  # type: ignore[index]

    def test_unknown_parent_is_fenced_first_with_exact_attempt_and_nonce(self) -> None:
        # Rebuild the fixture because prepare_unknown expects an empty store.
        self.tearDown()
        ExecutionStoreTestCase.setUp(self)
        self.ticket, _ = self.prepare_unknown()
        self.legs = self.store.get_legs("command-1")
        self.cloids = frozenset(item.cloid for item in self.legs)
        self._incident(
            code="UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING",
            incident_id="incident-unknown-parent",
        )
        attempt = self.store.get_attempt("command-1")
        snapshot = self._snapshot(size=None)

        result = self._controller().evaluate(snapshot, None, at=AT)

        self.assertIs(result.state, SafetyControllerState.QUEUED)
        self.assertEqual(result.reason_code, "NOOP_FENCE_QUEUED")
        assert result.prepared is not None
        self.assertEqual(result.prepared.evidence, attempt)
        action = result.prepared.action
        self.assertIsInstance(action, NoopFenceAction)
        assert isinstance(action, NoopFenceAction)
        self.assertEqual(action.original_nonce, attempt.nonce)
        self.assertEqual(action.attempt_id, attempt.attempt_id)
        self.assertEqual(action.action, {"type": "noop"})

        assert result.recovery_command is not None
        outbox = self.store.get_recovery_outbox(
            result.recovery_command.recovery_command_id
        )
        with (
            patch.object(
                self.store,
                "list_recovery_commands",
                return_value=(
                    replace(
                        result.recovery_command,
                        state="terminal",
                        terminal_at=AT,
                    ),
                ),
            ),
            patch.object(
                self.store,
                "list_recovery_outboxes",
                return_value=(replace(outbox, state="terminal"),),
            ),
        ):
            repeated = self._controller().evaluate(snapshot, None, at=AT)
        self.assertIs(repeated.state, SafetyControllerState.HALTED)
        self.assertIsNone(repeated.recovery_command)

    def test_terminal_prepared_noop_is_proven_unsent_and_can_be_replaced(self) -> None:
        controller, first, _ = self._terminal_prepared_noop_fixture()
        later = NOW + timedelta(seconds=11)
        snapshot = self._snapshot(size=None, at=later)

        replacement = controller.evaluate(snapshot, None, at=later)

        self.assertIs(replacement.state, SafetyControllerState.QUEUED)
        assert replacement.recovery_command is not None
        self.assertNotEqual(
            replacement.recovery_command.recovery_command_id,
            first.recovery_command_id,
        )
        self.assertEqual(
            [item.state for item in self.store.list_recovery_commands()],
            ["terminal", "queued"],
        )

    def test_terminal_noop_that_reached_sending_response_or_unknown_suppresses_repeat(self) -> None:
        controller, terminal, prepared = self._terminal_prepared_noop_fixture()
        later = NOW + timedelta(seconds=11)
        snapshot = self._snapshot(size=None, at=later)

        for state in ("sending", "response_received", "unknown"):
            with self.subTest(state=state), patch.object(
                self.store,
                "get_recovery_attempt",
                return_value=replace(
                    prepared,
                    state=state,
                    transport_evidence_hash="b" * 64,
                    updated_at=later,
                ),
            ):
                result = controller.evaluate(snapshot, None, at=later)
            self.assertIs(result.state, SafetyControllerState.HALTED)
            self.assertIsNone(result.recovery_command)
        self.assertEqual(
            self.store.get_recovery_command(terminal.recovery_command_id).state,
            "terminal",
        )
        self.assertEqual(self.store.list_recovery_commands(active_only=True), ())

    def test_globally_flat_account_cancels_only_remaining_owned_plan_orders(self) -> None:
        self._incident()
        stop = next(item.cloid for item in self.legs if item.role == "protective_stop")
        target = next(item.cloid for item in self.legs if item.role == "take_profit")
        snapshot = self._snapshot(
            size=None,
            orders=[
                raw_order(cloid=stop),
                raw_order(
                    oid=102,
                    cloid=target,
                    order_type="Take Market",
                    trigger_price="3000",
                    trigger_condition="Triggered above 3000",
                ),
            ],
        )

        result = self._controller().evaluate(snapshot, None, at=AT)

        self.assertIs(result.state, SafetyControllerState.QUEUED)
        assert result.prepared is not None
        self.assertIs(result.prepared.evidence, snapshot)
        action = result.prepared.action
        self.assertIsInstance(action, CancelByCloidAction)
        assert isinstance(action, CancelByCloidAction)
        self.assertEqual({item.cloid for item in action.requests}, {stop, target})
        self.assertEqual(action.action["type"], "cancelByCloid")

    def test_live_protective_stop_is_never_canceled(self) -> None:
        self._incident()
        stop = next(item.cloid for item in self.legs if item.role == "protective_stop")
        snapshot = self._snapshot(
            size="0.2",
            orders=[raw_order(cloid=stop, size="0.2", original_size="0.2")],
        )

        result = self._controller().evaluate(snapshot, None, at=AT)

        self.assertIs(result.state, SafetyControllerState.HALTED)
        self.assertIsNone(result.prepared)
        self.assertEqual(self.store.list_recovery_commands(), ())

    def test_foreign_stale_insufficient_depth_and_harmful_order_halt_without_authority(self) -> None:
        cases: list[tuple[str, object, dict[str, object] | None, datetime]] = []
        self._incident()
        cases.append(
            (
                "foreign",
                self._snapshot(size="0.2", orders=[raw_order(cloid=FOREIGN_CLOID)]),
                market_brief(AT),
                AT,
            )
        )
        cases.append(
            (
                "stale",
                self._snapshot(size="0.2"),
                market_brief(AT + timedelta(seconds=5)),
                AT + timedelta(seconds=5),
            )
        )
        cases.append(
            (
                "depth",
                self._snapshot(size="0.2"),
                market_brief(AT, bid_depth="0.1"),
                AT,
            )
        )
        entry = next(item.cloid for item in self.legs if item.role == "entry")
        cases.append(
            (
                "harmful",
                self._snapshot(
                    size="0.2",
                    orders=[
                        raw_order(
                            cloid=entry,
                            is_trigger=False,
                            reduce_only=False,
                            order_type="Limit",
                            side="B",
                            trigger_price="0",
                            trigger_condition="N/A",
                        )
                    ],
                ),
                market_brief(AT),
                AT,
            )
        )

        for label, snapshot, brief, at in cases:
            with self.subTest(label=label):
                result = self._controller().evaluate(snapshot, brief, at=at)  # type: ignore[arg-type]
                self.assertIs(result.state, SafetyControllerState.HALTED)
                self.assertIsNone(result.prepared)
        self.assertEqual(self.store.list_recovery_commands(), ())

    def test_flat_position_list_with_nonzero_margin_summary_halts(self) -> None:
        contradictory, _ = fetch_account(
            FixtureTransport(
                clearing=valid_clearing(
                    positions=[],
                    server_time=_milliseconds(AT) - 500,
                ),
                orders=[],
            ),
            received_at_ms=_milliseconds(AT),
            network="testnet",
        )

        result = self._controller().evaluate(contradictory, None, at=AT)

        self.assertIs(result.state, SafetyControllerState.HALTED)
        self.assertEqual(
            "ACCOUNT_FLAT_POSITION_SUMMARY_CONTRADICTION",
            result.reason_code,
        )

    def test_duplicate_and_concurrent_calls_queue_exactly_one_recovery(self) -> None:
        self._incident()
        snapshot = self._snapshot(size="0.2")
        controller = self._controller()
        results: list[object] = []
        barrier = threading.Barrier(3)

        def run() -> None:
            barrier.wait()
            results.append(controller.evaluate(snapshot, market_brief(AT), at=AT))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        states = {item.state for item in results}  # type: ignore[union-attr]
        self.assertEqual(
            states,
            {SafetyControllerState.QUEUED, SafetyControllerState.ACTIVE},
        )
        commands = self.store.list_recovery_commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            {item.recovery_command.recovery_command_id for item in results},  # type: ignore[union-attr]
            {commands[0].recovery_command_id},
        )
        self.assertTrue(all(item.prepared.evidence is snapshot for item in results))  # type: ignore[union-attr]

    def test_derived_close_cloid_collision_halts_without_issuing_authority(self) -> None:
        self._incident()
        snapshot = self._snapshot(size="0.2")
        entry_cloid = next(item.cloid for item in self.legs if item.role == "entry")

        with patch(
            "trading_harness.account_safety_controller.derive_recovery_close_cloid",
            return_value=entry_cloid,
        ):
            result = self._controller().evaluate(
                snapshot,
                market_brief(AT),
                at=AT,
            )

        self.assertIs(result.state, SafetyControllerState.HALTED)
        self.assertEqual(result.reason_code, "BOUNDED_FLATTEN_PREPARATION_REJECTED")
        self.assertEqual(self.store.list_recovery_commands(), ())

    def test_expired_unsent_recovery_is_terminalized_and_replaced_from_new_snapshot(self) -> None:
        self._incident()
        controller = self._controller()
        first_snapshot = self._snapshot(size="0.2", at=AT)
        first = controller.evaluate(first_snapshot, market_brief(AT), at=AT)
        self.assertIs(first.state, SafetyControllerState.QUEUED)

        later = AT + timedelta(seconds=11)
        second_snapshot = self._snapshot(size="0.1", at=later)
        second = controller.evaluate(
            second_snapshot,
            market_brief(later),
            at=later,
        )

        self.assertIs(second.state, SafetyControllerState.QUEUED)
        assert first.recovery_command is not None
        assert second.recovery_command is not None
        self.assertNotEqual(
            first.recovery_command.recovery_command_id,
            second.recovery_command.recovery_command_id,
        )
        commands = self.store.list_recovery_commands()
        self.assertEqual([item.state for item in commands], ["terminal", "queued"])
        assert second.prepared is not None
        self.assertIs(second.prepared.evidence, second_snapshot)
        action = second.prepared.action
        self.assertIsInstance(action, ReduceOnlyCloseAction)
        assert isinstance(action, ReduceOnlyCloseAction)
        self.assertEqual(action.close_size, Decimal("0.1"))

    def test_definitive_partial_close_uses_fresh_snapshot_derived_replacement_cloid(self) -> None:
        self._incident()
        controller = self._controller()
        first_snapshot = self._snapshot(size="0.2", at=AT)
        first = controller.evaluate(first_snapshot, market_brief(AT), at=AT)
        self.assertIs(first.state, SafetyControllerState.QUEUED)
        assert first.recovery_command is not None
        assert first.prepared is not None
        first_action = first.prepared.action
        assert isinstance(first_action, ReduceOnlyCloseAction)

        claim = self.store.claim_next_recovery(
            "recovery-worker",
            at=NOW + timedelta(seconds=7),
            lease_seconds=10,
        )
        assert claim is not None
        authority = self.store.require_recovery_signing_authority(
            first.recovery_command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8),
        )
        signed = self.make_signed_recovery(
            first.recovery_command,
            signing_authority_hash=authority.authority_hash,
        )
        attempt = self.store.prepare_recovery_attempt(
            first.recovery_command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="partial-close-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        self.store.require_recovery_submission_authority(
            first.recovery_command.recovery_command_id,
            attempt.attempt_id,
            signed.evidence_hash,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        transport = self.make_transport_evidence(
            attempt.attempt_id,
            signed,
            command_id=first.recovery_command.recovery_command_id,
            outcome="response_received",
            response_hash="a" * 64,
        )
        self.store.record_recovery_outcome(
            first.recovery_command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            at=NOW + timedelta(seconds=10),
        )
        reconciliation_claim = self.store.claim_recovery_reconciliation(
            first.recovery_command.recovery_command_id,
            "reconciler",
            at=NOW + timedelta(seconds=11),
            lease_seconds=10,
        )
        reconciliation_snapshot = self._snapshot(
            size="0.1",
            at=NOW + timedelta(seconds=12),
        )
        definitive_partial = replace(
            self.make_recovery_proof(
                first.recovery_command,
                observed_at=NOW + timedelta(seconds=12),
                complete=True,
                success=False,
            ),
            account_snapshot_hash=reconciliation_snapshot.snapshot_hash,
            signed_position_quantity=Decimal("0.1"),
            protected_quantity=Decimal("0"),
            proof_hash="",
        )
        terminal = self.store.reconcile_recovery(
            first.recovery_command.recovery_command_id,
            "reconciler",
            reconciliation_claim.fencing_token,
            reconciliation_id="definitive-partial-close",
            proof=definitive_partial,
            incident_resolution=None,
        )
        self.assertEqual(terminal.state, "terminal")

        later = NOW + timedelta(seconds=13)
        fresh_residual = self._snapshot(size="0.1", at=later)
        replacement = controller.evaluate(
            fresh_residual,
            market_brief(later),
            at=later,
        )

        self.assertIs(replacement.state, SafetyControllerState.QUEUED)
        assert replacement.prepared is not None
        replacement_action = replacement.prepared.action
        self.assertIsInstance(replacement_action, ReduceOnlyCloseAction)
        assert isinstance(replacement_action, ReduceOnlyCloseAction)
        self.assertEqual(
            first_action.cloid,
            derive_recovery_close_cloid(
                account_id="testnet-account",
                incident_id="incident-under-protected",
                position_snapshot_hash=first_snapshot.snapshot_hash,
            ),
        )
        self.assertEqual(
            replacement_action.cloid,
            derive_recovery_close_cloid(
                account_id="testnet-account",
                incident_id="incident-under-protected",
                position_snapshot_hash=fresh_residual.snapshot_hash,
            ),
        )
        self.assertNotEqual(first_action.cloid, replacement_action.cloid)
        self.assertEqual(replacement_action.close_size, Decimal("0.1"))
        self.assertNotIn(
            replacement_action.cloid,
            controller.signing_account.owned_cloids,
        )

    def test_policy_bounds_are_closed_and_mainnet_cannot_be_introduced(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at most 25"):
            SafetyControllerPolicy(max_flatten_slippage_bps=Decimal("25.1"))
        with self.assertRaises(TypeError):
            SafetyControllerPolicy(max_flatten_slippage_bps=25)  # type: ignore[arg-type]

        policy = self._controller().signer_policy
        mutated = deepcopy(policy.allowed_networks)
        self.assertNotIn(HyperliquidNetwork.MAINNET, mutated)


if __name__ == "__main__":
    unittest.main()
