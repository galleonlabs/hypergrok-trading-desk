from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from trading_harness import hyperliquid_transport
from trading_harness.errors import StateConflict
from trading_harness.domain import Environment
from trading_harness.execution_store import (
    EntrySubmissionAuthority,
    ExecutionStore,
)
from trading_harness.hyperliquid_transport import (
    HttpExchangeResponse,
    HyperliquidSubmissionError,
    RECOVERY_SUBMISSION_ENABLED,
    SubmissionOutcome,
    submit_signed_action,
)
from trading_harness.hyperliquid_signer import SignedActionEnvelope, sign_recovery_action
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from tests.test_execution_store import ExecutionStoreTestCase
from tests.test_hyperliquid_signer import (
    STORE_NOW,
    FakeNonceAllocator,
    FakeSigner,
    FakeWallet,
    NOW,
    make_signed,
    prepare_durable_recovery_fixture,
    prepare_durable_noop_fixture,
)


class FakeSender:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, endpoint: str, body: bytes, timeout: float) -> object:
        self.calls.append((endpoint, body, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def submit(signed, sender, *, clock, **kwargs):
    if isinstance(signed, SignedActionEnvelope) and "store" not in kwargs:
        temporary = tempfile.TemporaryDirectory()
        store = ExecutionStore(
            Path(temporary.name) / "execution.sqlite3",
            environment=Environment.TESTNET,
            account_id=signed.account_id,
            max_reserved_loss="100",
            max_reserved_notional="10000",
        )
        command_id = "command-1"
        attempt_id = "attempt-1"
        try:
            evidence_hash = signed.execution_store_evidence(command_id).evidence_hash
        except Exception:
            evidence_hash = "b" * 64
        authority = EntrySubmissionAuthority(
            command_id=command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=evidence_hash,
            nonce=signed.nonce,
            action_hash=signed.action_hash,
            wire_hash=signed.wire_hash,
            worker_id="transport-test-worker",
            fencing_token=1,
            lease_expires_at=NOW + timedelta(seconds=30),
            authority_hash="a" * 64,
        )
        kwargs = {
            "store": store,
            "command_id": command_id,
            "attempt_id": attempt_id,
            "signed_evidence_hash": evidence_hash,
            "worker_id": "transport-test-worker",
            "fencing_token": 1,
        }
        authority_patch = mock.patch.object(
            ExecutionStore,
            "require_submission_authority",
            return_value=authority,
        )
    else:
        temporary = None
        authority_patch = nullcontext()
    try:
        with (
            authority_patch,
            mock.patch.object(
                hyperliquid_transport,
                "_default_sender",
                side_effect=sender,
            ),
        ):
            return submit_signed_action(signed, clock=clock, **kwargs)
    finally:
        if temporary is not None:
            temporary.cleanup()


def successful_response(endpoint: str) -> HttpExchangeResponse:
    body = json.dumps(
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"filled": {"totalSz": "0.2", "avgPx": "2500", "oid": 1}},
                        {"resting": {"oid": 2}},
                        {"resting": {"oid": 3}},
                    ]
                },
            },
        }
    ).encode()
    return HttpExchangeResponse(status=200, final_url=endpoint, body=body)


class SingleAttemptTransportTests(unittest.TestCase):
    def test_protected_submission_requires_and_consumes_durable_authority(self) -> None:
        signed = make_signed()
        sender = FakeSender(successful_response(signed.exchange_url))
        with mock.patch.object(
            hyperliquid_transport, "_default_sender", side_effect=sender
        ):
            with self.assertRaisesRegex(
                HyperliquidSubmissionError, "durable authority"
            ):
                submit_signed_action(signed, clock=lambda: NOW)
        self.assertEqual([], sender.calls)

        with tempfile.TemporaryDirectory() as directory:
            store = ExecutionStore(
                Path(directory) / "execution.sqlite3",
                environment=Environment.TESTNET,
                account_id=signed.account_id,
                max_reserved_loss="100",
                max_reserved_notional="10000",
            )
            evidence = signed.execution_store_evidence("command-1")
            authority = EntrySubmissionAuthority(
                command_id="command-1",
                attempt_id="attempt-1",
                signed_evidence_hash=evidence.evidence_hash,
                nonce=signed.nonce,
                action_hash=signed.action_hash,
                wire_hash=signed.wire_hash,
                worker_id="worker-1",
                fencing_token=1,
                lease_expires_at=NOW + timedelta(seconds=30),
                authority_hash="c" * 64,
            )
            arguments = {
                "store": store,
                "command_id": "command-1",
                "attempt_id": "attempt-1",
                "signed_evidence_hash": evidence.evidence_hash,
                "worker_id": "worker-1",
                "fencing_token": 1,
            }
            with (
                mock.patch.object(
                    ExecutionStore,
                    "require_submission_authority",
                    side_effect=[
                        authority,
                        StateConflict("authority already consumed"),
                    ],
                ),
                mock.patch.object(
                    hyperliquid_transport,
                    "_default_sender",
                    side_effect=sender,
                ),
            ):
                submit_signed_action(signed, clock=lambda: NOW, **arguments)
                with self.assertRaises(StateConflict):
                    submit_signed_action(signed, clock=lambda: NOW, **arguments)
        self.assertEqual(1, len(sender.calls))

    def test_posts_exact_frozen_wire_once_to_network_endpoint(self) -> None:
        signed = make_signed()
        sender = FakeSender(successful_response(signed.exchange_url))

        attempt = submit(signed, sender, clock=lambda: NOW)

        self.assertEqual(
            sender.calls,
            [(signed.exchange_url, signed.wire_bytes, 10.0)],
        )
        self.assertIs(attempt.outcome, SubmissionOutcome.RESPONSE_RECEIVED)
        self.assertFalse(attempt.outcome_unknown)
        self.assertEqual(attempt.http_status, 200)
        self.assertEqual(attempt.detail_code, "response_received")
        self.assertEqual(attempt.send_count, 1)
        self.assertFalse(attempt.retry_performed)
        self.assertTrue(attempt.requires_reconciliation)
        self.assertEqual(attempt.response()["status"], "ok")  # type: ignore[index]
        self.assertRegex(attempt.response_hash or "", r"^[0-9a-f]{64}$")
        self.assertRegex(attempt.attempt_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(attempt.signed_envelope_hash, signed.envelope_hash)
        self.assertEqual(attempt.signer_binding_hash, signed.signer_binding_hash)
        attempt.verify_integrity()
        signed_evidence = signed.execution_store_evidence("command-1")
        persisted = attempt.execution_store_evidence(
            command_id="command-1",
            attempt_id="attempt-1",
            signed_evidence_hash=signed_evidence.evidence_hash,
        )
        self.assertEqual(persisted.transport_attempt_hash, attempt.attempt_hash)
        self.assertEqual(persisted.signed_evidence_hash, signed_evidence.evidence_hash)
        json.dumps(attempt.as_dict(), allow_nan=False, sort_keys=True)

    def test_timeout_is_unknown_and_is_never_retried(self) -> None:
        signed = make_signed()
        sender = FakeSender(TimeoutError("private timeout material"))

        attempt = submit(signed, sender, clock=lambda: NOW)

        self.assertEqual(len(sender.calls), 1)
        self.assertIs(attempt.outcome, SubmissionOutcome.UNKNOWN)
        self.assertTrue(attempt.outcome_unknown)
        self.assertEqual(attempt.detail_code, "timeout")
        self.assertIsNone(attempt.response())
        self.assertIsNone(attempt.response_hash)
        self.assertEqual(attempt.send_count, 1)
        self.assertFalse(attempt.retry_performed)
        self.assertNotIn("private timeout material", json.dumps(attempt.as_dict()))

    def test_non_200_redirect_oversize_and_bad_json_are_all_unknown(self) -> None:
        signed = make_signed()
        cases = (
            (
                HttpExchangeResponse(
                    status=503,
                    final_url=signed.exchange_url,
                    body=b"temporarily unavailable",
                ),
                "http_status_503",
            ),
            (
                HttpExchangeResponse(
                    status=200,
                    final_url="https://example.invalid/exchange",
                    body=b"{}",
                ),
                "redirect_refused",
            ),
            (
                HttpExchangeResponse(
                    status=200,
                    final_url=signed.exchange_url,
                    body=b"x" * (2 * 1024 * 1024 + 1),
                ),
                "response_too_large",
            ),
            (
                HttpExchangeResponse(
                    status=200,
                    final_url=signed.exchange_url,
                    body=b"not-json",
                ),
                "invalid_json_response",
            ),
            (
                HttpExchangeResponse(
                    status=200,
                    final_url=signed.exchange_url,
                    body=b'{"unsafeFloat":1.5}',
                ),
                "invalid_json_response",
            ),
            (
                HttpExchangeResponse(
                    status=200,
                    final_url=signed.exchange_url,
                    body=b'{"status":"ok","status":"err"}',
                ),
                "invalid_json_response",
            ),
        )
        for result, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                sender = FakeSender(result)
                attempt = submit(signed, sender, clock=lambda: NOW)
                self.assertEqual(len(sender.calls), 1)
                self.assertTrue(attempt.outcome_unknown)
                self.assertEqual(attempt.detail_code, expected_code)
                self.assertIsNone(attempt.response())

    def test_unexpected_transport_exception_is_sanitized_unknown(self) -> None:
        signed = make_signed()
        sender = FakeSender(OSError("secret network detail"))

        attempt = submit(signed, sender, clock=lambda: NOW)

        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(attempt.detail_code, "transport_OSError")
        self.assertNotIn("secret network detail", json.dumps(attempt.as_dict()))

    def test_invalid_sender_result_is_unknown_not_success(self) -> None:
        signed = make_signed()
        sender = FakeSender({"status": 200, "body": b"{}"})

        attempt = submit(signed, sender, clock=lambda: NOW)

        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(attempt.detail_code, "invalid_sender_result")
        self.assertTrue(attempt.requires_reconciliation)

    def test_tampered_signed_artifact_fails_before_any_network_call(self) -> None:
        signed = make_signed()
        tampered = replace(signed, wire_hash="0" * 64)
        sender = FakeSender(successful_response(signed.exchange_url))

        with self.assertRaisesRegex(HyperliquidSubmissionError, "integrity"):
            submit(tampered, sender, clock=lambda: NOW)

        self.assertEqual(sender.calls, [])

        rebound = replace(
            signed,
            main_account_address="0x" + "f" * 40,
        )
        with self.assertRaisesRegex(HyperliquidSubmissionError, "integrity"):
            submit(rebound, sender, clock=lambda: NOW)
        self.assertEqual(sender.calls, [])

    def test_attempt_hash_is_deterministic_for_same_record(self) -> None:
        signed = make_signed()
        first = submit(
            signed,
            FakeSender(successful_response(signed.exchange_url)),
            clock=lambda: NOW,
        )
        second = submit(
            signed,
            FakeSender(successful_response(signed.exchange_url)),
            clock=lambda: NOW,
        )

        self.assertEqual(first.attempt_hash, second.attempt_hash)

    def test_submission_attempt_tampering_is_detected(self) -> None:
        signed = make_signed()
        attempt = submit(
            signed,
            FakeSender(successful_response(signed.exchange_url)),
            clock=lambda: NOW,
        )

        for tampered in (
            replace(attempt, attempt_hash="0" * 64),
            replace(attempt, signer_binding_hash="0" * 64),
            replace(attempt, response_json='{"status":"err"}'),
            replace(attempt, retry_performed=True),
        ):
            with self.subTest(tampered=tampered):
                with self.assertRaises(HyperliquidSubmissionError):
                    tampered.verify_integrity()

    def test_expired_signed_action_is_refused_before_sender(self) -> None:
        signed = make_signed()
        sender = FakeSender(successful_response(signed.exchange_url))

        with self.assertRaisesRegex(HyperliquidSubmissionError, "expired"):
            submit(
                signed,
                sender,
                clock=lambda: NOW.replace(microsecond=0)
                + timedelta(seconds=5),
            )
        self.assertEqual(sender.calls, [])

    def test_mainnet_is_refused_before_integrity_or_sender(self) -> None:
        signed = replace(make_signed(), network=HyperliquidNetwork.MAINNET)
        sender = FakeSender(successful_response(signed.exchange_url))

        with self.assertRaisesRegex(HyperliquidSubmissionError, "mainnet"):
            submit(signed, sender, clock=lambda: NOW)
        self.assertEqual(sender.calls, [])


class DurableRecoveryTransportTests(ExecutionStoreTestCase):
    def prepare_signed(self):
        (
            recovery,
            snapshot,
            selected_policy,
            _,
            _,
            command,
            claim,
        ) = prepare_durable_recovery_fixture(self)
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=snapshot,
            policy=selected_policy,
            wallet=FakeWallet(),
            nonce_allocator=FakeNonceAllocator(
                [],
                int((STORE_NOW + timedelta(seconds=8)).timestamp() * 1_000) + 1,
            ),
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            sign_l1_action=FakeSigner([]),
        )
        evidence = signed.execution_store_evidence()
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="durable-recovery-attempt",
            signed_evidence=evidence,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=100),
        )
        return signed, evidence, attempt, command, claim

    def recovery_submit_arguments(self, evidence, attempt, claim):
        return {
            "store": self.store,
            "attempt_id": attempt.attempt_id,
            "signed_evidence_hash": evidence.evidence_hash,
            "worker_id": "recovery-worker",
            "fencing_token": claim.fencing_token,
        }

    def prepare_signed_noop(self):
        recovery, parent_attempt, selected_policy, _, command, claim = (
            prepare_durable_noop_fixture(self)
        )
        signed = sign_recovery_action(
            recovery,
            store=self.store,
            recovery_command_id=command.recovery_command_id,
            worker_id="recovery-worker",
            fencing_token=claim.fencing_token,
            evidence=parent_attempt,
            policy=selected_policy,
            wallet=FakeWallet(),
            nonce_allocator=None,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=1),
            sign_l1_action=FakeSigner([]),
        )
        evidence = signed.execution_store_evidence()
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="durable-noop-attempt",
            signed_evidence=evidence,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=100),
        )
        return signed, evidence, attempt, command, claim

    def test_consumed_submission_authority_binds_one_exact_sender_call(self) -> None:
        self.assertTrue(RECOVERY_SUBMISSION_ENABLED)
        signed, evidence, prepared, command, claim = self.prepare_signed()
        sender = FakeSender(successful_response(signed.exchange_url))
        arguments = self.recovery_submit_arguments(evidence, prepared, claim)

        result = submit(
            signed,
            sender,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
            **arguments,
        )

        self.assertEqual(sender.calls, [(signed.exchange_url, signed.wire_bytes, 10.0)])
        self.assertEqual(result.recovery_command_id, command.recovery_command_id)
        self.assertEqual(result.recovery_attempt_id, prepared.attempt_id)
        self.assertEqual(
            result.recovery_signed_evidence_hash,
            evidence.evidence_hash,
        )
        self.assertRegex(result.submission_authority_hash or "", r"^[0-9a-f]{64}$")
        with self.assertRaises(HyperliquidSubmissionError):
            replace(result, submission_authority_hash="0" * 64).verify_integrity()
        self.assertEqual(
            self.store.get_recovery_attempt(command.recovery_command_id).state,
            "sending",
        )
        transport_evidence = result.execution_store_evidence(
            command_id=command.recovery_command_id,
            attempt_id=prepared.attempt_id,
            signed_evidence_hash=evidence.evidence_hash,
        )
        with self.assertRaisesRegex(HyperliquidSubmissionError, "authority"):
            result.execution_store_evidence(
                command_id="forged-recovery-command",
                attempt_id=prepared.attempt_id,
                signed_evidence_hash=evidence.evidence_hash,
            )
        updated = self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport_evidence,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=300),
        )
        self.assertEqual(updated.state, "reconciling")

    def test_duplicate_submit_cannot_reach_sender_twice(self) -> None:
        signed, evidence, prepared, _, claim = self.prepare_signed()
        sender = FakeSender(successful_response(signed.exchange_url))
        arguments = self.recovery_submit_arguments(evidence, prepared, claim)
        submit(
            signed,
            sender,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
            **arguments,
        )

        with self.assertRaises(StateConflict):
            submit(
                signed,
                sender,
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=201),
                **arguments,
            )
        self.assertEqual(len(sender.calls), 1)

    def test_wrong_hash_worker_fence_and_tamper_fail_before_sender_or_authority(self) -> None:
        signed, evidence, prepared, command, claim = self.prepare_signed()
        sender = FakeSender(successful_response(signed.exchange_url))
        base = self.recovery_submit_arguments(evidence, prepared, claim)
        cases = (
            ({**base, "store": object()}, HyperliquidSubmissionError),
            ({**base, "signed_evidence_hash": "0" * 64}, HyperliquidSubmissionError),
            ({**base, "worker_id": "foreign-worker"}, StateConflict),
            ({**base, "fencing_token": claim.fencing_token + 1}, StateConflict),
        )
        for arguments, expected_error in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(expected_error):
                    submit(
                        signed,
                        sender,
                        clock=lambda: STORE_NOW
                        + timedelta(seconds=8, milliseconds=200),
                        **arguments,
                    )
                self.assertEqual(sender.calls, [])
                self.assertEqual(
                    self.store.get_recovery_attempt(command.recovery_command_id).state,
                    "prepared",
                )

        tampered = replace(signed, wire_hash="0" * 64)
        with self.assertRaisesRegex(HyperliquidSubmissionError, "integrity"):
            submit(
                tampered,
                sender,
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
                **base,
            )
        self.assertEqual(sender.calls, [])

    def test_expired_local_recovery_fails_without_consuming_prepared_attempt(self) -> None:
        signed, evidence, prepared, command, claim = self.prepare_signed()
        sender = FakeSender(successful_response(signed.exchange_url))
        with self.assertRaisesRegex(HyperliquidSubmissionError, "expired"):
            submit(
                signed,
                sender,
                clock=lambda: STORE_NOW + timedelta(seconds=15),
                **self.recovery_submit_arguments(evidence, prepared, claim),
            )
        self.assertEqual(sender.calls, [])
        self.assertEqual(
            self.store.get_recovery_attempt(command.recovery_command_id).state,
            "prepared",
        )

    def test_exact_noop_success_builds_atomic_response_evidence_and_cannot_replay(self) -> None:
        self.assertTrue(RECOVERY_SUBMISSION_ENABLED)
        self.assertTrue(hyperliquid_transport.NOOP_FENCE_SUBMISSION_ENABLED)
        signed, evidence, prepared, command, claim = self.prepare_signed_noop()
        sender = FakeSender(
            HttpExchangeResponse(
                status=200,
                final_url=signed.exchange_url,
                body=b'{"status":"ok","response":{"type":"default"}}',
            )
        )
        arguments = self.recovery_submit_arguments(evidence, prepared, claim)
        result = submit(
            signed,
            sender,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
            **arguments,
        )
        self.assertIs(result.outcome, SubmissionOutcome.RESPONSE_RECEIVED)
        transport = result.execution_store_evidence(
            command_id=command.recovery_command_id,
            attempt_id=prepared.attempt_id,
            signed_evidence_hash=evidence.evidence_hash,
        )
        noop = result.noop_fence_response_evidence(
            command.recovery_command_id,
            prepared.attempt_id,
            evidence.evidence_hash,
            parsed_at=STORE_NOW + timedelta(seconds=8, milliseconds=250),
        )
        self.assertEqual(noop.transport_evidence_hash, transport.evidence_hash)
        updated = self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            noop_response=noop,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=300),
        )
        self.assertEqual(updated.state, "reconciling")
        self.assertEqual(
            noop,
            self.store.get_noop_fence_response(command.recovery_command_id),
        )
        with self.assertRaises(StateConflict):
            submit(
                signed,
                sender,
                clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=301),
                **arguments,
            )
        self.assertEqual(len(sender.calls), 1)

    def test_wrong_noop_body_is_unknown_and_cannot_create_acceptance_evidence(self) -> None:
        signed, evidence, prepared, command, claim = self.prepare_signed_noop()
        sender = FakeSender(
            HttpExchangeResponse(
                status=200,
                final_url=signed.exchange_url,
                body=b'{"status":"ok","response":{"type":"order"}}',
            )
        )
        result = submit(
            signed,
            sender,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
            **self.recovery_submit_arguments(evidence, prepared, claim),
        )
        self.assertIs(result.outcome, SubmissionOutcome.UNKNOWN)
        self.assertEqual(
            result.detail_code, "noop_response_not_canonical_default"
        )
        with self.assertRaisesRegex(HyperliquidSubmissionError, "canonical"):
            result.noop_fence_response_evidence(
                command.recovery_command_id,
                prepared.attempt_id,
                evidence.evidence_hash,
                parsed_at=STORE_NOW + timedelta(seconds=8, milliseconds=250),
            )
        transport = result.execution_store_evidence(
            command_id=command.recovery_command_id,
            attempt_id=prepared.attempt_id,
            signed_evidence_hash=evidence.evidence_hash,
        )
        updated = self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            at=STORE_NOW + timedelta(seconds=8, milliseconds=300),
        )
        self.assertEqual(updated.state, "submitted_unknown")

    def test_timeout_noop_remains_unknown_without_false_response_evidence(self) -> None:
        signed, evidence, prepared, command, claim = self.prepare_signed_noop()
        sender = FakeSender(TimeoutError("unknown"))
        result = submit(
            signed,
            sender,
            clock=lambda: STORE_NOW + timedelta(seconds=8, milliseconds=200),
            **self.recovery_submit_arguments(evidence, prepared, claim),
        )
        self.assertIs(result.outcome, SubmissionOutcome.UNKNOWN)
        self.assertEqual(result.detail_code, "timeout")
        with self.assertRaises(HyperliquidSubmissionError):
            result.noop_fence_response_evidence(
                command.recovery_command_id,
                prepared.attempt_id,
                evidence.evidence_hash,
                parsed_at=STORE_NOW + timedelta(seconds=8, milliseconds=250),
            )


class HardenedDefaultSenderTests(unittest.TestCase):
    def test_default_sender_uses_post_timeout_and_redirect_rejecting_opener(self) -> None:
        signed = make_signed()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *arguments: object) -> None:
                del arguments

            def geturl(self) -> str:
                return signed.exchange_url

            def read(self, maximum: int) -> bytes:
                self.maximum = maximum
                return b"{}"

        response = Response()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            hyperliquid_transport.urlrequest,
            "build_opener",
            return_value=opener,
        ) as build:
            result = hyperliquid_transport._default_sender(  # type: ignore[attr-defined]
                signed.exchange_url,
                signed.wire_bytes,
                10.0,
            )

        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"{}")
        self.assertEqual(len(build.call_args.args), 2)
        self.assertIsInstance(
            build.call_args.args[0],
            hyperliquid_transport.urlrequest.ProxyHandler,
        )
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(
            build.call_args.args[1],
            hyperliquid_transport._RejectRedirectHandler,  # type: ignore[attr-defined]
        )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, signed.exchange_url)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, signed.wire_bytes)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10.0)
        self.assertEqual(response.maximum, 2 * 1024 * 1024 + 1)

    def test_default_sender_refuses_nonallowlisted_url_before_opening(self) -> None:
        with mock.patch.object(
            hyperliquid_transport.urlrequest,
            "build_opener",
        ) as build:
            with self.assertRaises(HyperliquidSubmissionError):
                hyperliquid_transport._default_sender(  # type: ignore[attr-defined]
                    "https://example.invalid/exchange",
                    b"{}",
                    10.0,
                )
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
