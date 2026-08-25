from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from trading_harness.execution_learning_sync import (
    ExecutionLearningProjector,
    LearningProjectionError,
)
from trading_harness.canonical import domain_hash
from trading_harness.execution_store import (
    LegReconciliation,
    RecoveryPermit,
    RecoveryVenueFill,
    VenueFill,
)
from trading_harness.learning_bridge import LearningRecorder
from trading_harness.learning_ledger import (
    ComponentVersions,
    DecisionClass,
    DecisionCycle,
    FillRole,
    LearningLedger,
    ProposedBracket,
    SourceEvidence,
    VenueFill as LearningVenueFill,
)
from trading_harness.post_trade_review import PostTradeReviewer
from tests.test_execution_store import ExecutionStoreTestCase, NOW, digest


class ExecutionLearningProjectorTests(ExecutionStoreTestCase):
    def _ledger(self) -> LearningLedger:
        return LearningLedger(Path(self.temporary.name) / "learning.sqlite3", clock=lambda: NOW + timedelta(minutes=1))

    def _decision(self, ticket, ledger: LearningLedger) -> str:
        assert ticket.plan is not None
        plan = ticket.plan
        entry = plan.entry
        stop = plan.protective_stop
        target = plan.take_profit
        assert entry.price_bound is not None
        assert stop.stop_price is not None
        assert target.stop_price is not None
        cycle_id = f"trade-{ticket.ticket_hash[:32]}"
        cycle = DecisionCycle(
            cycle_id=cycle_id,
            asset=entry.instrument,
            instrument=entry.instrument,
            venue=entry.venue,
            environment=entry.environment.value,
            timeframe="4h",
            decided_at=ticket.created_at,
            classification=DecisionClass(entry.side.value),
            rationale_code="test_infrastructure_learning",
            thesis_hash=ticket.assessment_hash,
            versions=ComponentVersions(
                strategy_version=entry.strategy_version,
                configuration_version="test-config-v1",
                code_hash=entry.code_hash,
                decision_rule_version="test-rule-v1",
                ta_version="test-ta-v1",
                sentiment_version="test-sentiment-v1",
                risk_policy_version=ticket.policy_version,
            ),
            evidence=(
                SourceEvidence(
                    source_id="analysis:test",
                    source_kind="asset_analysis",
                    content_hash=ticket.assessment_hash,
                    observed_at=ticket.created_at,
                    captured_at=ticket.created_at,
                    source_version="test-analysis-v1",
                ),
            ),
            bracket=ProposedBracket(
                side=DecisionClass(entry.side.value),
                entry_price=entry.price_bound,
                stop_price=stop.stop_price,
                target_price=target.stop_price,
                quantity=ticket.quantity,
                risk_amount=ticket.stressed_loss,
                settlement_asset="USDC",
            ),
            tags=("infrastructure_learning",),
        )
        ledger.record_decision(cycle, idempotency_key=f"decision:{cycle_id}")
        return cycle_id

    def test_repairs_references_and_projects_complete_fill_economics_idempotently(self) -> None:
        ticket, fencing = self.prepare_unknown()
        ledger = self._ledger()
        cycle_id = self._decision(ticket, ledger)
        projector = ExecutionLearningProjector(
            self.store,
            LearningRecorder(ledger),
            settlement_asset="USDC",
        )
        first = projector.synchronize()
        self.assertGreaterEqual(first.execution_references_inserted, 3)
        projector.require_entry_ready("command-1")

        legs = self.store.get_legs("command-1")
        fill = VenueFill(
            fill_id="hyperliquid:ETH:1:2:3",
            role="entry",
            cloid=legs[0].cloid,
            quantity=ticket.quantity,
            price=Decimal("2501"),
            fee=Decimal("0.25"),
            occurred_at=NOW + timedelta(seconds=4),
            venue_oid=101,
            venue_trade_id=2,
            transaction_hash="0x" + "a" * 64,
            closed_pnl=Decimal("0"),
            fee_token="USDC",
            observed_at=NOW + timedelta(seconds=5),
        )
        self.store.reconcile(
            "command-1",
            "reconciler",
            fencing,
            reconciliation_id="learning-reconciliation-1",
            account_snapshot_hash=digest("learning-snapshot"),
            observed_at=NOW + timedelta(seconds=5),
            complete=False,
            legs=(
                LegReconciliation(
                    "entry", legs[0].cloid, "filled", ticket.quantity, 101
                ),
                LegReconciliation(
                    "protective_stop", legs[1].cloid, "resting", "0", 102
                ),
                LegReconciliation(
                    "take_profit", legs[2].cloid, "resting", "0", 103
                ),
            ),
            signed_position_quantity=ticket.quantity,
            protected_quantity=ticket.quantity,
            fills=(fill,),
        )

        second = projector.synchronize()
        repeated = projector.synchronize()
        review = PostTradeReviewer(ledger).review_cycle(cycle_id)

        self.assertEqual(1, second.fills_inserted)
        self.assertEqual(0, repeated.fills_inserted)
        self.assertEqual(1, repeated.fills_existing)
        self.assertEqual(1, review.fill_count)
        self.assertEqual(Decimal("0.25"), review.total_fees)
        self.assertEqual(Decimal("0"), review.venue_reported_closed_pnl)
        self.assertEqual("USDC", review.venue_reported_closed_pnl_asset)
        self.assertIn(
            "funding_attribution_unverified", review.data_quality_flags
        )
        self.assertIsNotNone(review.quantity_weighted_slippage_bps)
        fill_event = next(
            event for event in ledger.events(cycle_id=cycle_id)
            if event.event_type == "venue_fill"
        )
        self.assertEqual("101", fill_event.payload["order_id"])
        self.assertEqual(legs[0].cloid, fill_event.payload["client_order_id"])

    def test_missing_staged_decision_blocks_projection_and_entry_readiness(self) -> None:
        self.admit_one()
        projector = ExecutionLearningProjector(
            self.store,
            LearningRecorder(self._ledger()),
            settlement_asset="USDC",
        )
        with self.assertRaises(LearningProjectionError):
            projector.synchronize()
        with self.assertRaisesRegex(Exception, "no staged learning decision"):
            projector.require_entry_ready("command-1")

    def test_recovery_close_economics_reach_learning_and_review(self) -> None:
        ticket, _ = self.admit_one()
        ledger = self._ledger()
        cycle_id = self._decision(ticket, ledger)
        incident = self.store.record_incident(
            incident_id="learning-recovery-incident",
            command_id="command-1",
            code="RECOVERY_REQUIRED",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )
        cloid = "0x" + "e" * 32
        close_quantity = ticket.quantity
        material = {
            "kind": "reduce_only_close",
            "original_signed_position": str(close_quantity),
            "close_size": str(close_quantity),
            "price_bound": "2400",
            "cloid": cloid,
            "symbol": "ETH",
            "action": {"type": "order"},
        }
        recovery_hash = domain_hash(
            "trading-harness/hyperliquid-recovery-action/v1", material
        )
        permit = RecoveryPermit(
            permit_id="learning-recovery-permit",
            token_hash=digest("learning-recovery-token"),
            parent_command_id="command-1",
            incident_id=incident.incident_id,
            kind="reduce_only_close",
            environment=self.store.environment,
            account_id=self.store.account_id,
            source_hash=digest("learning-recovery-source"),
            preflight_hash=None,
            recovery_hash=recovery_hash,
            recovery_material=material,
            safety_policy_hash=digest("learning-safety-policy"),
            original_attempt_id=None,
            original_nonce=None,
            issuer_id="learning-safety-authority",
            audience="learning-recovery-worker",
            issued_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(seconds=16),
        )
        self.store.register_recovery_permit(permit)
        command = self.store.queue_recovery(
            recovery_command_id="learning-recovery-command",
            permit_id=permit.permit_id,
            token_hash=permit.token_hash,
            audience=permit.audience,
            at=NOW + timedelta(seconds=7),
        )
        claim = self.store.claim_next_recovery(
            "learning-recovery-worker",
            at=NOW + timedelta(seconds=8),
            lease_seconds=10,
        )
        assert claim is not None
        authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "learning-recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        signed = self.make_signed_recovery(
            command, signing_authority_hash=authority.authority_hash
        )
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "learning-recovery-worker",
            claim.fencing_token,
            attempt_id="learning-recovery-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        self.store.require_recovery_submission_authority(
            command.recovery_command_id,
            attempt.attempt_id,
            signed.evidence_hash,
            "learning-recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        transport = self.make_transport_evidence(
            attempt.attempt_id,
            signed,
            command_id=command.recovery_command_id,
            outcome="response_received",
            response_hash=digest("learning-recovery-response"),
        )
        self.store.record_recovery_outcome(
            command.recovery_command_id,
            "learning-recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            at=NOW + timedelta(seconds=10),
        )
        recon_claim = self.store.claim_recovery_reconciliation(
            command.recovery_command_id,
            "learning-reconciler",
            at=NOW + timedelta(seconds=11),
            lease_seconds=10,
        )
        proof = self.make_recovery_proof(
            command,
            observed_at=NOW + timedelta(seconds=12),
            complete=True,
            success=True,
        )
        fill = RecoveryVenueFill(
            fill_id=(
                f"hyperliquid:ETH:"
                f"{int((NOW + timedelta(seconds=10)).timestamp() * 1_000)}:20:30"
            ),
            recovery_command_id=command.recovery_command_id,
            parent_command_id=command.parent_command_id,
            cloid=cloid,
            symbol="ETH",
            side="sell",
            quantity=close_quantity,
            signed_quantity=-close_quantity,
            start_position=close_quantity,
            end_position=Decimal("0"),
            price=Decimal("2395"),
            fee=Decimal("0.40"),
            closed_pnl=Decimal("-5"),
            fee_token="USDC",
            crossed=True,
            builder_fee=None,
            venue_oid=30,
            venue_trade_id=20,
            transaction_hash="0x" + "b" * 64,
            occurred_at=NOW + timedelta(seconds=10),
            observed_at=proof.observed_at,
            account_snapshot_hash=proof.account_snapshot_hash,
            venue_evidence_hash=digest("learning-recovery-venue-read"),
        )
        self.store.reconcile_recovery(
            command.recovery_command_id,
            "learning-reconciler",
            recon_claim.fencing_token,
            reconciliation_id="learning-recovery-reconciliation",
            proof=proof,
            incident_resolution="contained",
            fills=(fill,),
        )

        projector = ExecutionLearningProjector(
            self.store,
            LearningRecorder(ledger),
            settlement_asset="USDC",
        )
        parent = self.store.get_command("command-1")
        projector._ensure_references(parent)
        assert ticket.plan is not None
        entry_price = ticket.plan.entry.price_bound
        assert entry_price is not None
        entry_cloid = self.store.get_legs("command-1")[0].cloid
        ledger.record_fill(
            LearningVenueFill(
                cycle_id=cycle_id,
                command_id="command-1",
                fill_id="learning-parent-entry",
                order_id="10",
                client_order_id=entry_cloid,
                role=FillRole.ENTRY,
                side=DecisionClass.BUY,
                venue_occurred_at=NOW + timedelta(seconds=4),
                observed_at=NOW + timedelta(seconds=5),
                price=entry_price,
                reference_price=entry_price,
                quantity=close_quantity,
                fee=Decimal("0.25"),
                fee_asset="USDC",
                venue_evidence_hash=digest("learning-parent-entry-evidence"),
                venue_closed_pnl=Decimal("0"),
                venue_pnl_asset="USDC",
            ),
            idempotency_key="learning-parent-entry",
        )
        projected = projector.synchronize()
        repeated = projector.synchronize()
        review = PostTradeReviewer(ledger).review_cycle(cycle_id)

        self.assertEqual(1, projected.fills_inserted)
        self.assertEqual(0, repeated.fills_inserted)
        self.assertEqual(1, repeated.fills_existing)
        self.assertEqual(2, review.fill_count)
        self.assertEqual(Decimal("0.65"), review.total_fees)
        self.assertEqual(Decimal("-5"), review.venue_reported_closed_pnl)
        fill_event = next(
            event
            for event in ledger.events(cycle_id=cycle_id)
            if event.event_type == "venue_fill"
            and event.payload["fill_id"] == fill.fill_id
        )
        self.assertEqual("exit", fill_event.payload["role"])
        self.assertEqual(cloid, fill_event.payload["client_order_id"])
