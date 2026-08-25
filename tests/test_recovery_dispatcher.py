from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest import mock

from trading_harness import hyperliquid_transport, recovery_dispatcher
from trading_harness.errors import RecordNotFound
from trading_harness.execution_store import ExecutionStore
from trading_harness.hyperliquid_recovery import RecoveryKind
from trading_harness.hyperliquid_transport import (
    HttpExchangeResponse,
    SubmissionOutcome,
)
from trading_harness.recovery_dispatcher import (
    DurableRecoverySigner,
    PreparedRecovery,
    RecoveryDispatchError,
    RecoveryExecutionDispatcher,
)
from tests.test_execution_store import ExecutionStoreTestCase
from tests.test_hyperliquid_signer import (
    STORE_NOW,
    FakeNonceAllocator,
    FakeSigner,
    FakeWallet,
    prepare_durable_noop_fixture,
    prepare_durable_recovery_fixture,
)
from tests.test_hyperliquid_transport import FakeSender, successful_response


class FixturePreparer:
    def __init__(self, prepared: PreparedRecovery) -> None:
        self.prepared = prepared
        self.calls: list[tuple[object, datetime]] = []

    def prepare(self, command, *, at: datetime) -> PreparedRecovery:
        self.calls.append((command, at))
        return self.prepared


class CountingSigner:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def sign(self, action, **kwargs):
        self.calls += 1
        return self.inner.sign(action, **kwargs)


class ForgingSigner(CountingSigner):
    def sign(self, action, **kwargs):
        signed = super().sign(action, **kwargs)
        return replace(signed, recovery_hash="0" * 64)


class SignThenCrash(CountingSigner):
    def sign(self, action, **kwargs):
        super().sign(action, **kwargs)
        raise RuntimeError("simulated process loss after signing")


class CrashingPreparer:
    def prepare(self, command, *, at: datetime):
        del command, at
        raise RuntimeError("simulated read-side crash")


def exact_noop_response(endpoint: str) -> HttpExchangeResponse:
    return HttpExchangeResponse(
        status=200,
        final_url=endpoint,
        body=b'{"status":"ok","response":{"type":"default"}}',
    )


def wrong_noop_response(endpoint: str) -> HttpExchangeResponse:
    return HttpExchangeResponse(
        status=200,
        final_url=endpoint,
        body=b'{"status":"ok","response":{"type":"order"}}',
    )


class RecoveryDispatcherTests(ExecutionStoreTestCase):
    def close_fixture(self):
        fixture = prepare_durable_recovery_fixture(self, lease_seconds=1)
        recovery, snapshot, policy, _, _, command, _ = fixture
        at = STORE_NOW + timedelta(seconds=9)
        events: list[str] = []
        signer = CountingSigner(
            DurableRecoverySigner(
                policy=policy,
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(
                    events,
                    int(at.timestamp() * 1_000) + 1,
                ),
                sign_l1_action=FakeSigner(events),
            )
        )
        preparer = FixturePreparer(PreparedRecovery(recovery, snapshot))
        dispatcher = RecoveryExecutionDispatcher(
            self.store,
            worker_id="production-recovery-worker",
            preparer=preparer,
            signer=signer,
            clock=lambda: at,
            lease_seconds=30,
        )
        return dispatcher, preparer, signer, events, command, at

    def noop_fixture(self):
        recovery, attempt, policy, _, command, _ = prepare_durable_noop_fixture(
            self
        )
        at = STORE_NOW + timedelta(seconds=15)
        events: list[str] = []
        signer = CountingSigner(
            DurableRecoverySigner(
                policy=policy,
                wallet=FakeWallet(),
                nonce_allocator=FakeNonceAllocator(events),
                sign_l1_action=FakeSigner(events),
            )
        )
        preparer = FixturePreparer(PreparedRecovery(recovery, attempt))
        dispatcher = RecoveryExecutionDispatcher(
            self.store,
            worker_id="production-recovery-worker",
            preparer=preparer,
            signer=signer,
            clock=lambda: at,
            lease_seconds=30,
        )
        return dispatcher, preparer, signer, events, command, at

    def test_close_success_persists_every_boundary_and_requires_reconciliation(self) -> None:
        dispatcher, preparer, signer, events, command, _ = self.close_fixture()
        sender = FakeSender(
            successful_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            result = dispatcher.dispatch_next()

        assert result is not None
        self.assertEqual(command.recovery_command_id, result.recovery_command_id)
        self.assertIs(result.kind, RecoveryKind.REDUCE_ONLY_CLOSE)
        self.assertIs(result.outcome, SubmissionOutcome.RESPONSE_RECEIVED)
        self.assertEqual("reconciling", result.state)
        self.assertTrue(result.requires_reconciliation)
        self.assertFalse(result.retry_allowed)
        self.assertIsNone(result.noop_response_evidence_hash)
        self.assertEqual(1, len(preparer.calls))
        self.assertEqual(1, signer.calls)
        self.assertEqual(["nonce_committed", "signed"], events)
        self.assertEqual(1, len(sender.calls))
        persisted_signed = self.store.get_signed_recovery_evidence(
            command.recovery_command_id
        )
        persisted_attempt = self.store.get_recovery_attempt(
            command.recovery_command_id
        )
        self.assertEqual(result.signed_evidence_hash, persisted_signed.evidence_hash)
        self.assertEqual(result.attempt_id, persisted_attempt.attempt_id)
        self.assertEqual("response_received", persisted_attempt.state)

    def test_exact_noop_success_persists_response_evidence_atomically(self) -> None:
        dispatcher, _, signer, events, command, _ = self.noop_fixture()
        sender = FakeSender(
            exact_noop_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            result = dispatcher.dispatch_next()

        assert result is not None
        self.assertIs(result.kind, RecoveryKind.NOOP_FENCE)
        self.assertIs(result.outcome, SubmissionOutcome.RESPONSE_RECEIVED)
        self.assertEqual("reconciling", result.state)
        self.assertIsNotNone(result.noop_response_evidence_hash)
        persisted = self.store.get_noop_fence_response(
            command.recovery_command_id
        )
        self.assertEqual(result.noop_response_evidence_hash, persisted.evidence_hash)
        self.assertEqual([], [event for event in events if event == "nonce_committed"])
        self.assertEqual(["signed"], events)
        self.assertEqual(1, signer.calls)
        self.assertEqual(1, len(sender.calls))

    def test_wrong_noop_body_is_unknown_without_acceptance_evidence(self) -> None:
        dispatcher, _, _, _, command, _ = self.noop_fixture()
        sender = FakeSender(
            wrong_noop_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            result = dispatcher.dispatch_next()

        assert result is not None
        self.assertIs(result.outcome, SubmissionOutcome.UNKNOWN)
        self.assertEqual("submitted_unknown", result.state)
        self.assertIsNone(result.noop_response_evidence_hash)
        with self.assertRaises(RecordNotFound):
            self.store.get_noop_fence_response(command.recovery_command_id)
        self.assertEqual("unknown", self.store.get_recovery_attempt(
            command.recovery_command_id
        ).state)
        self.assertEqual(
            "noop_response_not_canonical_default",
            self.store.get_recovery_transport_evidence(
                command.recovery_command_id
            ).detail_code,
        )

    def test_duplicate_dispatch_never_reaches_sender_twice(self) -> None:
        dispatcher, _, signer, _, _, _ = self.close_fixture()
        sender = FakeSender(
            successful_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            first = dispatcher.dispatch_next()
            second = dispatcher.dispatch_next()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, signer.calls)
        self.assertEqual(1, len(sender.calls))

    def test_forged_preparer_is_rejected_before_signer_or_sender(self) -> None:
        dispatcher, _, signer, _, command, at = self.close_fixture()
        original_prepared = dispatcher.preparer.prepared
        forged_action = replace(
            original_prepared.action,
            close_size=Decimal("0.1"),
        )
        dispatcher.preparer = FixturePreparer(
            PreparedRecovery(forged_action, original_prepared.evidence)
        )
        sender = FakeSender(
            successful_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            with self.assertRaises(RecoveryDispatchError):
                dispatcher.dispatch_next()
        self.assertEqual(0, signer.calls)
        self.assertEqual([], sender.calls)
        outbox = self.store.get_recovery_outbox(command.recovery_command_id)
        self.assertEqual("claimed", outbox.state)
        self.assertEqual(at + timedelta(seconds=30), outbox.lease_expires_at)

    def test_forged_signer_output_is_rejected_after_single_use_authority(self) -> None:
        dispatcher, _, signer, _, command, _ = self.close_fixture()
        dispatcher.signer = ForgingSigner(signer.inner)
        sender = FakeSender(
            successful_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ):
            with self.assertRaises(RecoveryDispatchError):
                dispatcher.dispatch_next()
        self.assertEqual([], sender.calls)
        self.assertEqual(
            "signing",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_attempt(command.recovery_command_id)

    def test_crash_before_signing_expires_unsent_without_attempt_or_retry(self) -> None:
        dispatcher, _, _, _, command, at = self.close_fixture()
        dispatcher.preparer = CrashingPreparer()
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch_next()
        self.assertEqual(
            "claimed",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )
        reclaimed = self.store.claim_next_recovery(
            "replacement-worker",
            at=at + timedelta(seconds=30),
            lease_seconds=5,
        )
        self.assertIsNone(reclaimed)
        self.assertEqual(
            "terminal",
            self.store.get_recovery_command(command.recovery_command_id).state,
        )
        self.assertEqual(
            "terminal",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_attempt(command.recovery_command_id)

    def test_crash_after_signing_terminalizes_proven_unsent_and_never_resigns(self) -> None:
        dispatcher, _, signer, _, command, at = self.close_fixture()
        crash_signer = SignThenCrash(signer.inner)
        dispatcher.signer = crash_signer
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch_next()
        self.assertEqual(1, crash_signer.calls)
        self.assertEqual(
            "signing",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )
        self.assertIsNone(
            self.store.claim_next_recovery(
                "replacement-worker",
                at=at + timedelta(seconds=30),
                lease_seconds=5,
            )
        )
        self.assertEqual(
            "terminal",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )

    def test_crash_before_submission_authority_terminalizes_proven_unsent(self) -> None:
        dispatcher, _, _, _, command, at = self.close_fixture()
        with mock.patch.object(
            recovery_dispatcher,
            "submit_signed_action",
            side_effect=RuntimeError("simulated crash before transport"),
        ):
            with self.assertRaises(RuntimeError):
                dispatcher.dispatch_next()
        persisted = self.store.get_recovery_attempt(command.recovery_command_id)
        self.assertEqual("prepared", persisted.state)
        self.assertRegex(
            self.store.get_signed_recovery_evidence(
                command.recovery_command_id
            ).evidence_hash,
            r"^[0-9a-f]{64}$",
        )
        self.assertIsNone(
            self.store.claim_next_recovery(
                "replacement-worker",
                at=at + timedelta(seconds=30),
                lease_seconds=5,
            )
        )
        self.assertEqual(
            "prepared",
            self.store.get_recovery_attempt(command.recovery_command_id).state,
        )
        self.assertEqual(
            "terminal",
            self.store.get_recovery_command(command.recovery_command_id).state,
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_transport_evidence(
                command.recovery_command_id
            )

    def test_crash_after_send_never_sends_again_and_normalizes_unknown(self) -> None:
        dispatcher, _, signer, _, command, at = self.close_fixture()
        sender = FakeSender(
            successful_response("https://api.hyperliquid-testnet.xyz/exchange")
        )
        with mock.patch.object(
            hyperliquid_transport,
            "_default_sender",
            side_effect=sender,
        ), mock.patch.object(
            ExecutionStore,
            "record_recovery_outcome",
            side_effect=RuntimeError("simulated crash after HTTP"),
        ):
            with self.assertRaises(RuntimeError):
                dispatcher.dispatch_next()
        self.assertEqual(1, len(sender.calls))
        self.assertEqual(1, signer.calls)
        self.assertEqual(
            "sending",
            self.store.get_recovery_attempt(command.recovery_command_id).state,
        )
        self.assertIsNone(
            self.store.claim_next_recovery(
                "replacement-worker",
                at=at + timedelta(seconds=30),
                lease_seconds=5,
            )
        )
        self.assertEqual(
            "unknown",
            self.store.get_recovery_attempt(command.recovery_command_id).state,
        )
        self.assertEqual(1, len(sender.calls))


if __name__ == "__main__":
    unittest.main()
