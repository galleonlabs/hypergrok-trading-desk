from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.dispatcher import DispatchPackage, ExecutionDispatcher
from trading_harness.domain import Environment
from trading_harness.errors import (
    AdmissionDenied,
    EntrySubmissionRevoked,
    RecordNotFound,
    StateConflict,
)
from trading_harness.execution_store import DispatchPreflight, ExecutionStore
from trading_harness.hyperliquid_signer import (
    SignerPolicy,
    SigningAccount,
    sign_protected_action,
)
from trading_harness.hyperliquid_transport import (
    HttpExchangeResponse,
)
from trading_harness.hyperliquid_wire import (
    HyperliquidNetwork,
    build_protected_order_action,
)
from tests.test_execution_store import (
    NOW,
    digest,
    make_approval,
    make_infrastructure_grant,
    make_ticket,
)
from tests.test_hyperliquid_signer import FakeNonceAllocator, FakeSigner, FakeWallet
from tests.test_hyperliquid_wire import metadata


MAIN_ACCOUNT = "0x" + "a" * 40
SIGNER = "0x" + "b" * 40


class StepClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(milliseconds=10)
        return value


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ExecutionStore(
            Path(self.temporary.name) / "execution.sqlite3",
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        self.ticket = make_ticket()
        grant = make_infrastructure_grant(self.ticket)
        self.store.register_infrastructure_grant(grant, at=NOW)
        self.store.register_ticket(
            self.ticket,
            infrastructure_grant_hash=grant.grant_hash,
            stored_at=NOW + timedelta(milliseconds=1),
        )
        approval = make_approval(self.ticket)
        self.store.register_approval(approval)
        self.store.admit(
            command_id="command-1",
            approval_id=approval.approval_id,
            token_hash=approval.token_hash,
            audience=approval.audience,
            at=NOW + timedelta(milliseconds=3),
        )
        self.clock = StepClock(NOW + timedelta(seconds=1))
        self.signing_events: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, command, ticket, plan, at):
        unsigned = build_protected_order_action(
            plan,
            metadata(),
            network=HyperliquidNetwork.TESTNET,
            at=at,
        )
        preflight = DispatchPreflight(
            command_id=command.command_id,
            ticket_hash=ticket.ticket_hash,
            plan_hash=plan.plan_hash,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            account_snapshot_hash=digest("fresh-account"),
            account_server_time_ms=int(
                (at - timedelta(milliseconds=500)).timestamp() * 1_000
            ),
            metadata_hash=unsigned.metadata_hash,
            market_snapshot_hash=digest("fresh-market"),
            risk_policy_hash=ticket.policy_hash,
            observed_at=at,
            expires_at=at + timedelta(seconds=5),
            passed=True,
        )
        return DispatchPackage(
            preflight=preflight,
            metadata=metadata(),
            protected_action=unsigned,
        )

    def sign(self, unsigned, plan, selected_metadata, preflight):
        now = NOW + timedelta(seconds=1, milliseconds=30)
        policy = SignerPolicy(
            accounts=(
                SigningAccount(
                    account_id="testnet-account",
                    main_account_address=MAIN_ACCOUNT,
                    signer_address=SIGNER,
                ),
            ),
            allowed_asset_ids=frozenset({1}),
        )
        return sign_protected_action(
            unsigned,
            plan=plan,
            metadata=selected_metadata,
            preflight=preflight,
            policy=policy,
            wallet=FakeWallet(SIGNER),
            nonce_allocator=FakeNonceAllocator(
                self.signing_events,
                nonce=int(now.timestamp() * 1000) + 1,
            ),
            clock=lambda: now,
            sign_l1_action=FakeSigner(self.signing_events),
        )

    def response_body(self) -> bytes:
        size = str(self.ticket.plan.entry.quantity.normalize())
        return json.dumps(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": size,
                                    "avgPx": "2500",
                                    "oid": 1,
                                }
                            },
                            {"resting": {"oid": 2}},
                            {"resting": {"oid": 3}},
                        ]
                    },
                },
            },
            separators=(",", ":"),
        ).encode()

    def rejected_stop_body(self) -> bytes:
        size = str(self.ticket.plan.entry.quantity.normalize())
        return json.dumps(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": size,
                                    "avgPx": "2500",
                                    "oid": 1,
                                }
                            },
                            {"error": "stop rejected"},
                            {"resting": {"oid": 3}},
                        ]
                    },
                },
            },
            separators=(",", ":"),
        ).encode()

    def dispatcher(self):
        return ExecutionDispatcher(
            self.store,
            preparer=self.prepare,
            signer=self.sign,
            clock=self.clock,
            lease_seconds=15,
        )

    def test_full_pipeline_persists_before_one_send_then_requires_reconciliation(self) -> None:
        sends: list[bytes] = []

        def sender(endpoint, body, _timeout):
            sends.append(body)
            return HttpExchangeResponse(
                200,
                endpoint,
                self.response_body(),
            )

        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=sender,
        ):
            result = self.dispatcher().dispatch_next("dispatcher")

        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "response_received")
        self.assertEqual(result.command_state, "reconciling")
        self.assertTrue(result.venue_write_attempted)
        self.assertFalse(result.retry_performed)
        self.assertEqual(len(sends), 1)
        attempt = self.store.get_attempt("command-1")
        self.assertEqual(attempt.preflight_hash, result.preflight_hash)
        self.assertEqual(attempt.nonce, result.nonce)
        self.assertEqual(attempt.wire_hash, result.wire_hash)
        self.assertEqual(
            [leg.status for leg in self.store.get_legs("command-1")],
            ["filled", "resting", "resting"],
        )
        self.assertIsNone(self.dispatcher().dispatch_next("dispatcher-2"))

    def test_permanent_preflight_denial_voids_unsent_command_without_signing(self) -> None:
        def deny(*_arguments):
            raise AdmissionDenied("TEST_DENIAL", "permanent fixture denial")

        dispatcher = ExecutionDispatcher(
            self.store,
            preparer=deny,
            signer=self.sign,
            clock=self.clock,
            lease_seconds=15,
        )
        result = dispatcher.dispatch_next("dispatcher")

        assert result is not None
        self.assertEqual("preflight_denied", result.outcome)
        self.assertEqual("TEST_DENIAL", result.detail_code)
        self.assertFalse(result.venue_write_attempted)
        self.assertEqual("terminal", self.store.get_command("command-1").state)
        self.assertEqual(
            (Decimal("0"), Decimal("0")), self.store.get_reserved_exposure()
        )
        self.assertEqual("terminal", self.store.get_outbox("command-1").state)
        self.assertEqual([], self.signing_events)
        self.assertIsNone(dispatcher.dispatch_next("dispatcher-2"))

    def test_timeout_becomes_unknown_without_retry(self) -> None:
        def timeout(_endpoint, _body, _timeout):
            raise TimeoutError("private timeout")

        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=timeout,
        ):
            result = self.dispatcher().dispatch_next("dispatcher")
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.command_state, "submitted_unknown")
        self.assertEqual(result.detail_code, "timeout")
        self.assertFalse(result.retry_performed)
        self.assertEqual(self.store.get_attempt("command-1").state, "unknown")

    def test_revoked_final_submission_guard_blocks_after_preflight_and_signing(self) -> None:
        revoked = False

        def prepare_and_revoke(command, ticket, plan, at):
            nonlocal revoked
            package = self.prepare(command, ticket, plan, at)
            revoked = True
            return package

        @contextmanager
        def guard():
            if revoked:
                raise EntrySubmissionRevoked(
                    "runtime submission capability was revoked"
                )
            yield

        dispatcher = ExecutionDispatcher(
            self.store,
            preparer=prepare_and_revoke,
            signer=self.sign,
            clock=self.clock,
            lease_seconds=15,
            submission_guard=guard,
        )
        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=AssertionError("revoked entry must not reach sender"),
        ):
            result = dispatcher.dispatch_next("dispatcher")

        assert result is not None
        self.assertEqual("submission_revoked", result.outcome)
        self.assertFalse(result.venue_write_attempted)
        self.assertEqual("terminal", self.store.get_command("command-1").state)
        self.assertEqual("prepared", self.store.get_attempt("command-1").state)
        with self.assertRaises(RecordNotFound):
            self.store.get_transport_evidence("command-1")

    def test_filled_entry_rejected_stop_returns_critical_recovery_state(self) -> None:
        def sender(endpoint, _body, _timeout):
            return HttpExchangeResponse(200, endpoint, self.rejected_stop_body())

        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=sender,
        ):
            result = self.dispatcher().dispatch_next("dispatcher")

        self.assertEqual(result.outcome, "response_received")
        self.assertTrue(result.recovery_required)
        self.assertTrue(result.incident_ids)
        self.assertEqual(self.store.get_protection("command-1").state, "failed")
        self.assertIn(
            "PROTECTION_SUBMISSION_FAILED",
            {incident.code for incident in self.store.list_incidents("command-1")},
        )

    def test_unparseable_http_200_order_response_becomes_unknown(self) -> None:
        def malformed(endpoint, _body, _timeout):
            return HttpExchangeResponse(200, endpoint, b"{}")

        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=malformed,
        ):
            result = self.dispatcher().dispatch_next("dispatcher")
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(
            result.detail_code,
            "response_unparseable_or_unrecordable",
        )
        self.assertEqual(self.store.get_attempt("command-1").state, "unknown")

    def test_sender_exception_after_prepared_attempt_is_sanitized_unknown(self) -> None:
        def broken(_endpoint, _body, _timeout):
            raise RuntimeError("PRIVATE TRANSPORT MATERIAL")

        with patch(
            "trading_harness.hyperliquid_transport._default_sender",
            side_effect=broken,
        ):
            result = self.dispatcher().dispatch_next("dispatcher")

        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.detail_code, "transport_RuntimeError")
        self.assertNotIn("PRIVATE", repr(result.as_dict()))
        self.assertEqual(self.store.get_attempt("command-1").state, "unknown")

    def test_signing_failure_voids_unsent_command_and_requires_new_authority(self) -> None:
        calls = 0

        def bad_signer(_unsigned, _plan, _metadata, _preflight):
            raise RuntimeError("signing failed")

        def sender(_endpoint, _body, _timeout):
            nonlocal calls
            calls += 1
            raise AssertionError("must not send")

        dispatcher = ExecutionDispatcher(
            self.store,
            preparer=self.prepare,
            signer=bad_signer,
            clock=self.clock,
            lease_seconds=5,
        )
        with (
            patch(
                "trading_harness.hyperliquid_transport._default_sender",
                side_effect=sender,
            ),
            self.assertRaisesRegex(RuntimeError, "signing failed"),
        ):
            dispatcher.dispatch_next("dispatcher")

        self.assertEqual(calls, 0)
        with self.assertRaises(Exception):
            self.store.get_attempt("command-1")
        self.assertEqual(self.store.get_command("command-1").state, "terminal")
        self.assertEqual(self.store.get_reserved_exposure(), (0, 0))
        self.assertIsNone(
            self.store.claim_next(
                "replacement",
                at=NOW + timedelta(seconds=7),
                lease_seconds=5,
            )
        )


if __name__ == "__main__":
    unittest.main()
