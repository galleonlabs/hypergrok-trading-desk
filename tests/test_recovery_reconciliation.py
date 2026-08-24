from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
import tempfile
import unittest

from trading_harness.canonical import canonical_json, domain_hash
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_store import (
    DispatchPreflight,
    ExecutionStore,
    LegReconciliation,
    NoopFenceResponseEvidence,
    RecoveryPermit,
    SignedEnvelopeEvidence,
    SignedRecoveryEvidence,
    TransportOutcomeEvidence,
)
from trading_harness.hyperliquid_account import OrderSide, fetch_account_snapshot
from trading_harness.hyperliquid_reconcile import (
    FillCoverage,
    ParsedOrderStatus,
    SignedFillEvidence,
    VenueReconciliationBundle,
    VenueOrderState,
    VENUE_RECONCILIATION_HASH_DOMAIN,
)
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.recovery_reconciliation import (
    RecoveryReconciliationCoordinator,
    RecoveryVenueRead,
)
from trading_harness.reconciliation_coordinator import (
    MainEntryReconciliationCoordinator,
    _bundle_material,
)
from tests.test_account_risk import flat_clearing
from tests.test_execution_store import NOW, digest, make_approval, make_ticket
from tests.test_hyperliquid_account import (
    ACCOUNT,
    FixtureTransport,
    raw_position,
    valid_clearing,
)


CLOSE_CLOID = "0x" + "c" * 32
CANCEL_CLOID = "0x" + "d" * 32
ACCOUNT_ID = "testnet-recovery-desk"


def fresh_flat_snapshot(at):
    clearing = flat_clearing()
    clearing["time"] = int((at - timedelta(milliseconds=500)).timestamp() * 1_000)
    return fetch_account_snapshot(
        ACCOUNT,
        "testnet",
        transport=FixtureTransport(clearing=clearing, orders=[]),
        clock=lambda: at,
    )


def fresh_unprotected_long_snapshot(at):
    clearing = valid_clearing(positions=[raw_position(signed_size="0.5")])
    clearing["time"] = int(
        (at - timedelta(milliseconds=500)).timestamp() * 1_000
    )
    return fetch_account_snapshot(
        ACCOUNT,
        "testnet",
        transport=FixtureTransport(clearing=clearing, orders=[]),
        clock=lambda: at,
    )


def complete_coverage() -> FillCoverage:
    return FillCoverage(
        requested_start_time_ms=1,
        requested_end_time_ms=2,
        page_count=1,
        page_limit=2_000,
        retention_limit=10_000,
        returned_rows=1,
        unique_fills=1,
        duplicate_fills=0,
        unmatched_fills=0,
        page_saturated=False,
        retention_limited=False,
        complete=True,
        reason="complete",
    )


def empty_complete_coverage() -> FillCoverage:
    return FillCoverage(
        requested_start_time_ms=1,
        requested_end_time_ms=2,
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
        reason="complete",
    )


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ExecutionStore(
            Path(self.temporary.name) / "execution.sqlite",
            environment=Environment.TESTNET,
            account_id=ACCOUNT_ID,
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        ticket = make_ticket(account_id=ACCOUNT_ID)
        self.ticket = ticket
        self.store.register_ticket(
            ticket, stored_at=NOW + timedelta(milliseconds=1)
        )
        approval = make_approval(ticket, account_id=ACCOUNT_ID)
        self.store.register_approval(approval)
        self.store.admit(
            command_id="command-1",
            approval_id=approval.approval_id,
            token_hash=approval.token_hash,
            audience=approval.audience,
            at=NOW + timedelta(milliseconds=3),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def queue_response_recovery(
        self,
        kind: str,
        material: dict[str, object],
        *,
        original_attempt=None,
    ):
        incident = self.store.record_incident(
            incident_id=f"incident-{kind}",
            command_id="command-1",
            code="RECOVERY_REQUIRED",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )
        recovery_hash = domain_hash(
            "trading-harness/hyperliquid-recovery-action/v1", material
        )
        permit = RecoveryPermit(
            permit_id=f"permit-{kind}",
            token_hash=digest(f"permit-token-{kind}"),
            parent_command_id="command-1",
            incident_id=incident.incident_id,
            kind=kind,
            environment=Environment.TESTNET,
            account_id=ACCOUNT_ID,
            source_hash=digest(f"source-{kind}"),
            preflight_hash=(
                None if original_attempt is None else original_attempt.preflight_hash
            ),
            recovery_hash=recovery_hash,
            recovery_material=material,
            safety_policy_hash=digest("safety-policy"),
            original_attempt_id=(
                None if original_attempt is None else original_attempt.attempt_id
            ),
            original_nonce=(
                None if original_attempt is None else original_attempt.nonce
            ),
            issuer_id="safety-authority",
            audience="recovery-worker",
            issued_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(seconds=16),
        )
        self.store.register_recovery_permit(permit)
        command = self.store.queue_recovery(
            recovery_command_id=f"recovery-{kind}",
            permit_id=permit.permit_id,
            token_hash=permit.token_hash,
            audience=permit.audience,
            at=NOW + timedelta(seconds=7),
        )
        claim = self.store.claim_next_recovery(
            "recovery-dispatcher",
            at=NOW + timedelta(seconds=8),
            lease_seconds=10,
        )
        assert claim is not None
        authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "recovery-dispatcher",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        signed = SignedRecoveryEvidence(
            recovery_command_id=command.recovery_command_id,
            incident_id=command.incident_id,
            kind=command.kind,
            source_hash=command.source_hash,
            recovery_hash=command.recovery_hash,
            signing_authority_hash=authority.authority_hash,
            safety_policy_hash=command.safety_policy_hash,
            nonce=(
                888 if original_attempt is None else original_attempt.nonce
            ),
            wire_hash=digest("wire"),
            action_hash=digest("action"),
            signature_hash=digest("signature"),
            envelope_hash=digest("envelope"),
            signer_binding_hash=digest("binding"),
            expires_after_ms=int((NOW + timedelta(seconds=15)).timestamp() * 1_000),
            signed_at_ms=int((NOW + timedelta(seconds=8)).timestamp() * 1_000),
        )
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-dispatcher",
            claim.fencing_token,
            attempt_id=f"attempt-{kind}",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        self.store.require_recovery_submission_authority(
            command.recovery_command_id,
            attempt.attempt_id,
            signed.evidence_hash,
            "recovery-dispatcher",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        noop_body = {"status": "ok", "response": {"type": "default"}}
        response_hash = (
            domain_hash(
                "trading-harness/hyperliquid-submission-response/v1",
                noop_body,
            )
            if kind == "noop_fence"
            else digest("response")
        )
        transport = TransportOutcomeEvidence(
            command_id=command.recovery_command_id,
            attempt_id=attempt.attempt_id,
            signed_evidence_hash=signed.evidence_hash,
            endpoint="https://api.hyperliquid-testnet.xyz/exchange",
            attempted_at_ms=int((NOW + timedelta(seconds=10)).timestamp() * 1_000),
            outcome="response_received",
            http_status=200,
            detail_code="response_received",
            response_hash=response_hash,
            transport_attempt_hash=digest("transport"),
            send_count=1,
            retry_performed=False,
            venue_write_attempted=True,
        )
        noop_response = (
            NoopFenceResponseEvidence(
                recovery_command_id=command.recovery_command_id,
                attempt_id=attempt.attempt_id,
                signed_evidence_hash=signed.evidence_hash,
                transport_evidence_hash=transport.evidence_hash,
                nonce=signed.nonce,
                response_json=canonical_json(noop_body),
                response_hash=response_hash,
                parsed_at=NOW + timedelta(seconds=10),
            )
            if kind == "noop_fence"
            else None
        )
        self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-dispatcher",
            claim.fencing_token,
            transport_evidence=transport,
            noop_response=noop_response,
            at=NOW + timedelta(seconds=10),
        )
        return command, transport

    def prepare_parent_unknown(self):
        claim = self.store.claim_next(
            "dispatcher",
            at=NOW + timedelta(seconds=1),
            lease_seconds=10,
        )
        assert claim is not None
        assert self.ticket.plan is not None
        preflight = DispatchPreflight(
            command_id="command-1",
            ticket_hash=self.ticket.ticket_hash,
            plan_hash=self.ticket.plan.plan_hash,
            environment=Environment.TESTNET,
            account_id=ACCOUNT_ID,
            account_snapshot_hash=digest("entry-account"),
            metadata_hash=digest("entry-metadata"),
            market_snapshot_hash=digest("entry-market"),
            risk_policy_hash=self.ticket.policy_hash,
            observed_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=20),
            passed=True,
        )
        self.store.register_preflight(
            preflight,
            at=NOW + timedelta(seconds=1, milliseconds=1),
        )
        signed = SignedEnvelopeEvidence(
            command_id="command-1",
            preflight_hash=preflight.preflight_hash,
            environment=Environment.TESTNET,
            endpoint="https://api.hyperliquid-testnet.xyz/exchange",
            account_id=ACCOUNT_ID,
            plan_hash=preflight.plan_hash,
            action_hash=digest("entry-action"),
            nonce=1_777_777_777_777,
            wire_hash=digest("entry-wire"),
            signature_hash=digest("entry-signature"),
            envelope_hash=digest("entry-envelope"),
            signer_binding_hash=digest("entry-binding"),
            authorization_expires_at_ms=int(
                preflight.expires_at.timestamp() * 1_000
            ),
            expires_after_ms=int(preflight.expires_at.timestamp() * 1_000),
            signed_at_ms=int(
                (NOW + timedelta(seconds=1)).timestamp() * 1_000
            ),
        )
        attempt = self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="entry-attempt",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=signed.nonce,
            action_hash=signed.action_hash,
            wire_hash=signed.wire_hash,
            at=NOW + timedelta(seconds=2),
        )
        unknown = TransportOutcomeEvidence(
            command_id="command-1",
            attempt_id=attempt.attempt_id,
            signed_evidence_hash=signed.evidence_hash,
            endpoint="https://api.hyperliquid-testnet.xyz/exchange",
            attempted_at_ms=int(
                (NOW + timedelta(seconds=2, milliseconds=500)).timestamp()
                * 1_000
            ),
            outcome="unknown",
            http_status=None,
            detail_code="socket_closed_after_write",
            response_hash=None,
            transport_attempt_hash=digest("entry-transport"),
            send_count=1,
            retry_performed=False,
            venue_write_attempted=True,
        )
        self.store.mark_submitted_unknown(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            transport_evidence=unknown,
            at=NOW + timedelta(seconds=3),
        )
        return self.store.get_attempt("command-1")

    def test_close_derives_flat_terminal_proof_from_exact_fill_chain(self) -> None:
        material = {
            "kind": "reduce_only_close",
            "main_account_address": ACCOUNT,
            "symbol": "ETH",
            "cloid": CLOSE_CLOID,
            "original_signed_position": "1",
            "close_size": "1",
            "action": {"type": "order"},
        }
        command, transport = self.queue_response_recovery(
            "reduce_only_close", material
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_flat_snapshot(at)
        status = ParsedOrderStatus(
            role="entry",
            requested_cloid=CLOSE_CLOID,
            state=VenueOrderState.ORDER,
            venue_status="filled",
            status_timestamp_ms=int(
                (at - timedelta(seconds=1)).timestamp() * 1_000
            ),
            oid=501,
            symbol="ETH",
            remaining_size=Decimal("0"),
            original_size=Decimal("1"),
            is_trigger=False,
            reduce_only=True,
        )
        fill = SignedFillEvidence(
            fill_id="close-fill",
            role="entry",
            cloid=CLOSE_CLOID,
            oid=501,
            tid=1,
            transaction_hash="0x" + "1" * 64,
            symbol="ETH",
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            signed_quantity=Decimal("-1"),
            start_position=Decimal("1"),
            end_position=Decimal("0"),
            price=Decimal("2500"),
            fee=Decimal("0.25"),
            closed_pnl=Decimal("0"),
            fee_token="USDC",
            crossed=True,
            builder_fee=None,
            time_ms=int((at - timedelta(seconds=1)).timestamp() * 1_000),
        )
        evidence = RecoveryVenueRead(
            network="testnet",
            account_id=ACCOUNT_ID,
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=at,
            order_statuses=(status,),
            signed_fills=(fill,),
            fill_coverage=complete_coverage(),
        )
        result = RecoveryReconciliationCoordinator(self.store).reconcile(
            command.recovery_command_id,
            "reconciler",
            snapshot=snapshot,
            evidence=evidence,
            at=at,
        )
        self.assertTrue(result.proof.complete)
        self.assertTrue(result.proof.success)
        self.assertEqual("terminal", result.recovery_state)
        self.assertEqual("contained", result.incident_resolution)
        self.assertGreater(self.store.get_reserved_exposure()[0], Decimal("0"))

    def test_close_discontinuous_or_overclose_evidence_stays_incomplete(self) -> None:
        material = {
            "kind": "reduce_only_close",
            "main_account_address": ACCOUNT,
            "symbol": "ETH",
            "cloid": CLOSE_CLOID,
            "original_signed_position": "1",
            "close_size": "0.5",
            "action": {"type": "order"},
        }
        command, transport = self.queue_response_recovery(
            "reduce_only_close", material
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_flat_snapshot(at)
        status = ParsedOrderStatus(
            "entry", CLOSE_CLOID, VenueOrderState.ORDER, "filled",
            int((at - timedelta(seconds=1)).timestamp() * 1_000),
            501, "ETH", Decimal("0"),
            Decimal("0.5"), False, True,
        )
        fill = SignedFillEvidence(
            "bad-fill", "entry", CLOSE_CLOID, 501, 1,
            "0x" + "2" * 64, "ETH", OrderSide.SELL,
            Decimal("1"), Decimal("-1"), Decimal("1"), Decimal("0"),
            Decimal("2500"), Decimal("0.25"), Decimal("0"), "USDC",
            True, None, int((at - timedelta(seconds=1)).timestamp() * 1_000),
        )
        evidence = RecoveryVenueRead(
            "testnet", ACCOUNT_ID, snapshot.snapshot_hash, at,
            (status,), (fill,), complete_coverage(),
        )
        result = RecoveryReconciliationCoordinator(self.store).reconcile(
            command.recovery_command_id,
            "reconciler",
            snapshot=snapshot,
            evidence=evidence,
            transport=transport,
            at=at,
        )
        self.assertFalse(result.proof.complete)
        self.assertIn(
            "recovery_close_fill_quantity_mismatch",
            result.incomplete_reasons,
        )
        self.assertEqual("reconciling", result.recovery_state)

    def test_cancel_requires_exact_requested_cloid_absent(self) -> None:
        material = {
            "kind": "cancel_by_cloid",
            "main_account_address": ACCOUNT,
            "requests": [{"cloid": CANCEL_CLOID}],
            "action": {"type": "cancelByCloid"},
        }
        command, transport = self.queue_response_recovery(
            "cancel_by_cloid", material
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_flat_snapshot(at)
        status = ParsedOrderStatus(
            "entry", CANCEL_CLOID, VenueOrderState.ORDER, "canceled",
            int((at - timedelta(seconds=1)).timestamp() * 1_000),
            601, "ETH", Decimal("0"),
            Decimal("1"), False, False,
        )
        evidence = RecoveryVenueRead(
            "testnet", ACCOUNT_ID, snapshot.snapshot_hash, at,
            (status,), (), empty_complete_coverage(),
        )
        result = RecoveryReconciliationCoordinator(self.store).reconcile(
            command.recovery_command_id,
            "reconciler",
            snapshot=snapshot,
            evidence=evidence,
            transport=transport,
            at=at,
        )
        self.assertTrue(result.proof.success)
        self.assertEqual((CANCEL_CLOID,), result.proof.affected_cloids)

    def test_cancel_cannot_remove_live_stop_from_unprotected_position(self) -> None:
        stop_cloid = next(
            leg.cloid
            for leg in self.store.get_legs("command-1")
            if leg.role == "protective_stop"
        )
        material = {
            "kind": "cancel_by_cloid",
            "main_account_address": ACCOUNT,
            "requests": [{"cloid": stop_cloid}],
            "action": {"type": "cancelByCloid"},
        }
        command, transport = self.queue_response_recovery(
            "cancel_by_cloid", material
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_unprotected_long_snapshot(at)
        status = ParsedOrderStatus(
            "protective_stop",
            stop_cloid,
            VenueOrderState.ORDER,
            "canceled",
            int((at - timedelta(seconds=1)).timestamp() * 1_000),
            601,
            "ETH",
            Decimal("0"),
            Decimal("0.5"),
            True,
            True,
        )
        evidence = RecoveryVenueRead(
            "testnet",
            ACCOUNT_ID,
            snapshot.snapshot_hash,
            at,
            (status,),
            (),
            empty_complete_coverage(),
        )
        result = RecoveryReconciliationCoordinator(self.store).reconcile(
            command.recovery_command_id,
            "reconciler",
            snapshot=snapshot,
            evidence=evidence,
            transport=transport,
            at=at,
        )
        self.assertFalse(result.proof.complete)
        self.assertIn(
            "cancel_would_remove_live_protective_stop",
            result.incomplete_reasons,
        )
        self.assertIn(
            "post_cancel_position_not_fully_protected",
            result.incomplete_reasons,
        )

    def test_transport_must_match_persisted_attempt_hash(self) -> None:
        material = {
            "kind": "cancel_by_cloid",
            "main_account_address": ACCOUNT,
            "requests": [{"cloid": CANCEL_CLOID}],
            "action": {"type": "cancelByCloid"},
        }
        command, transport = self.queue_response_recovery(
            "cancel_by_cloid", material
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_flat_snapshot(at)
        status = ParsedOrderStatus(
            "entry",
            CANCEL_CLOID,
            VenueOrderState.ORDER,
            "canceled",
            int((at - timedelta(seconds=1)).timestamp() * 1_000),
            601,
            "ETH",
            Decimal("0"),
            Decimal("1"),
            False,
            False,
        )
        evidence = RecoveryVenueRead(
            "testnet",
            ACCOUNT_ID,
            snapshot.snapshot_hash,
            at,
            (status,),
            (),
            empty_complete_coverage(),
        )
        with self.assertRaisesRegex(StateConflict, "snapshot hash"):
            RecoveryReconciliationCoordinator(self.store).reconcile(
                command.recovery_command_id,
                "reconciler",
                snapshot=replace(snapshot, withdrawable=Decimal("999999")),
                evidence=evidence,
                transport=transport,
                at=at,
            )
        substituted = TransportOutcomeEvidence(
            command_id=transport.command_id,
            attempt_id=transport.attempt_id,
            signed_evidence_hash=transport.signed_evidence_hash,
            endpoint=transport.endpoint,
            attempted_at_ms=transport.attempted_at_ms,
            outcome=transport.outcome,
            http_status=transport.http_status,
            detail_code="substituted_detail",
            response_hash=transport.response_hash,
            transport_attempt_hash=transport.transport_attempt_hash,
            send_count=transport.send_count,
            retry_performed=False,
            venue_write_attempted=True,
        )
        with self.assertRaises(StateConflict):
            RecoveryReconciliationCoordinator(self.store).reconcile(
                command.recovery_command_id,
                "reconciler",
                snapshot=snapshot,
                evidence=evidence,
                transport=substituted,
                at=at,
            )

    def test_noop_default_success_definitively_fences_missing_original(self) -> None:
        original_attempt = self.prepare_parent_unknown()
        material = {
            "kind": "noop_fence",
            "main_account_address": ACCOUNT,
            "attempt_id": original_attempt.attempt_id,
            "preflight_hash": original_attempt.preflight_hash,
            "original_nonce": original_attempt.nonce,
            "original_action_hash": original_attempt.action_hash,
            "original_wire_hash": original_attempt.wire_hash,
            "action": {"type": "noop"},
        }
        command, transport = self.queue_response_recovery(
            "noop_fence",
            material,
            original_attempt=original_attempt,
        )
        at = NOW + timedelta(seconds=12)
        snapshot = fresh_flat_snapshot(at)
        statuses = tuple(
            ParsedOrderStatus(
                leg.role,
                leg.cloid,
                VenueOrderState.MISSING,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            for leg in self.store.get_legs("command-1")
        )
        evidence = RecoveryVenueRead(
            "testnet",
            ACCOUNT_ID,
            snapshot.snapshot_hash,
            at,
            statuses,
            (),
            empty_complete_coverage(),
        )
        accepted = self.store.get_noop_fence_response(
            command.recovery_command_id
        )
        with self.assertRaisesRegex(ValidationError, "canonical default"):
            NoopFenceResponseEvidence(
                recovery_command_id=accepted.recovery_command_id,
                attempt_id=accepted.attempt_id,
                signed_evidence_hash=accepted.signed_evidence_hash,
                transport_evidence_hash=accepted.transport_evidence_hash,
                nonce=accepted.nonce,
                response_json=canonical_json(
                    {"status": "err", "response": "invalid nonce"}
                ),
                response_hash=accepted.response_hash,
                parsed_at=accepted.parsed_at,
            )
        result = RecoveryReconciliationCoordinator(self.store).reconcile(
            command.recovery_command_id,
            "reconciler",
            snapshot=snapshot,
            evidence=evidence,
            transport=transport,
            at=at,
        )
        self.assertTrue(result.proof.complete)
        self.assertTrue(result.proof.success)
        self.assertEqual(original_attempt.nonce, result.proof.resolved_original_nonce)
        self.assertEqual("fenced", result.proof.resolved_original_outcome)
        self.assertEqual("terminal", result.recovery_state)
        self.assertEqual("contained", result.incident_resolution)
        self.assertIsNone(result.required_schema_change)
        resolution = self.store.require_terminal_noop_fence("command-1")
        self.assertEqual(original_attempt.nonce, resolution.original_nonce)
        self.assertEqual(result.proof.proof_hash, resolution.proof_hash)
        self.assertEqual(command.recovery_command_id, resolution.recovery_command_id)

        later = NOW + timedelta(seconds=14)
        later_snapshot = fresh_flat_snapshot(later)
        observed_at = datetime.fromtimestamp(
            later_snapshot.server_time_ms / 1_000,
            tz=NOW.tzinfo,
        )
        later_statuses = tuple(
            ParsedOrderStatus(
                leg.role,
                leg.cloid,
                VenueOrderState.MISSING,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            for leg in self.store.get_legs("command-1")
        )
        later_coverage = FillCoverage(
            requested_start_time_ms=later_snapshot.server_time_ms - 60_000,
            requested_end_time_ms=later_snapshot.server_time_ms,
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
        provisional = VenueReconciliationBundle(
            network=HyperliquidNetwork.TESTNET,
            main_account_address=ACCOUNT,
            account_id=ACCOUNT_ID,
            command_id="command-1",
            plan_hash=self.store.get_command("command-1").plan_hash,
            account_snapshot_hash=later_snapshot.snapshot_hash,
            observed_at=observed_at,
            order_statuses=later_statuses,
            signed_fills=(),
            fill_coverage=later_coverage,
            legs=tuple(
                LegReconciliation(
                    role=leg.role,
                    cloid=leg.cloid,
                    status="absent",
                    cumulative_filled=Decimal("0"),
                    venue_oid=None,
                )
                for leg in self.store.get_legs("command-1")
            ),
            fills=(),
            signed_position_quantity=Decimal("0"),
            protected_quantity=Decimal("0"),
            complete=False,
            incomplete_reasons=tuple(
                f"{role}_order_missing"
                for role in ("entry", "protective_stop", "take_profit")
            ),
            reconciliation_hash="0" * 64,
        )
        fenced_bundle = replace(
            provisional,
            reconciliation_hash=domain_hash(
                VENUE_RECONCILIATION_HASH_DOMAIN,
                _bundle_material(provisional),
            ),
        )
        main_claim = self.store.claim_reconciliation(
            "command-1",
            "main-reconciler",
            at=observed_at,
            lease_seconds=10,
        )
        main_result = MainEntryReconciliationCoordinator(
            self.store,
            network=HyperliquidNetwork.TESTNET,
            clock=lambda: later,
        ).apply_bundle(
            fenced_bundle,
            later_snapshot,
            worker_id="main-reconciler",
            fencing_token=main_claim.fencing_token,
            reconciliation_id="fenced-parent-flat",
        )
        self.assertTrue(main_result.evidence_complete)
        self.assertTrue(main_result.terminal)
        self.assertEqual(Decimal("0"), main_result.account_reserved_loss)
        self.assertEqual((), main_result.active_incident_ids)
        self.assertEqual(
            "closed",
            self.store.list_incidents("command-1")[0].state,
        )


if __name__ == "__main__":
    unittest.main()
