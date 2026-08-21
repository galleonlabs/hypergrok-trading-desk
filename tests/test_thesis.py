from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from trading_harness.domain import (
    DeploymentGrant,
    Environment,
    GrantType,
    OrderType,
    SemanticIntent,
    Side,
)
from trading_harness.thesis import (
    EvidenceReview,
    EvidenceStatus,
    EvidenceTransitionError,
    ThesisVersion,
    apply_evidence_review,
    assess_deployment,
)


NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)


def thesis(status: EvidenceStatus = EvidenceStatus.DRAFT) -> ThesisVersion:
    return ThesisVersion(
        thesis_id="sma-20-100-nasdaq-30m",
        thesis_version="1",
        strategy_version="scanner-1",
        code_hash="b" * 64,
        author_id="researcher-alice",
        title="Nasdaq 20/100 SMA cross on completed 30-minute bars",
        rationale="Test whether the pre-registered crossover has net forward edge.",
        created_at=NOW,
        evidence_status=status,
        specification_hash="c" * 64,
    )


def review(
    current: EvidenceStatus,
    target: EvidenceStatus,
    *,
    reviewer_id: str = "reviewer-bob",
) -> EvidenceReview:
    return EvidenceReview(
        review_id=f"review-{current.value}-{target.value}",
        thesis_id="sma-20-100-nasdaq-30m",
        thesis_version="1",
        code_hash="b" * 64,
        from_status=current,
        to_status=target,
        reviewer_id=reviewer_id,
        reviewed_at=NOW + timedelta(hours=1),
        evidence_artifact_hash="d" * 64,
        reason="Predeclared gate and independent reproducibility checks passed.",
    )


def active_grant(**changes: object) -> DeploymentGrant:
    values: dict[str, object] = {
        "grant_id": "grant-testnet-001",
        "thesis_id": "sma-20-100-nasdaq-30m",
        "thesis_version": "1",
        "strategy_version": "scanner-1",
        "code_hash": "b" * 64,
        "venue": "hyperliquid",
        "account_id": "strategy-testnet-account",
        "environment": Environment.TESTNET,
        "grant_type": GrantType.STRATEGY_TESTNET,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "allowed_instruments": ("ETH-PERP",),
        "allowed_actions": ("open",),
        "max_notional": Decimal("1000"),
        "max_loss": Decimal("50"),
        "approver_ids": ("risk-owner",),
    }
    values.update(changes)
    return DeploymentGrant(**values)


def scoped_intent(**changes: object) -> SemanticIntent:
    values: dict[str, object] = {
        "intent_id": "intent-001",
        "thesis_id": "sma-20-100-nasdaq-30m",
        "thesis_version": "1",
        "strategy_version": "scanner-1",
        "code_hash": "b" * 64,
        "venue": "hyperliquid",
        "account_id": "strategy-testnet-account",
        "environment": Environment.TESTNET,
        "instrument": "ETH-PERP",
        "action": "open",
        "side": Side.BUY,
        "quantity": "0.1",
        "order_type": OrderType.MARKET,
        "expires_at": NOW + timedelta(minutes=1),
        "client_order_id": "01J5THESIS001",
        "price_bound": "3010",
    }
    values.update(changes)
    return SemanticIntent(**values)


class EvidenceStateTests(unittest.TestCase):
    def test_evidence_progresses_only_through_registered_gates(self) -> None:
        record = thesis()
        path = (
            EvidenceStatus.REGISTERED,
            EvidenceStatus.EXPLORATORY_TESTED,
            EvidenceStatus.HOLDOUT_PASSED,
            EvidenceStatus.SHADOW_CONFIRMED,
            EvidenceStatus.VALIDATED,
        )

        for next_status in path:
            old_status = record.evidence_status
            record = apply_evidence_review(record, review(old_status, next_status))

        self.assertIs(record.evidence_status, EvidenceStatus.VALIDATED)

    def test_draft_cannot_jump_directly_to_validated(self) -> None:
        with self.assertRaisesRegex(EvidenceTransitionError, "illegal evidence transition"):
            apply_evidence_review(
                thesis(), review(EvidenceStatus.DRAFT, EvidenceStatus.VALIDATED)
            )

    def test_author_cannot_validate_own_evidence(self) -> None:
        with self.assertRaisesRegex(EvidenceTransitionError, "cannot validate"):
            apply_evidence_review(
                thesis(EvidenceStatus.SHADOW_CONFIRMED),
                review(
                    EvidenceStatus.SHADOW_CONFIRMED,
                    EvidenceStatus.VALIDATED,
                    reviewer_id="researcher-alice",
                ),
            )

    def test_stale_or_wrong_version_review_is_rejected(self) -> None:
        stale = review(EvidenceStatus.REGISTERED, EvidenceStatus.EXPLORATORY_TESTED)
        with self.assertRaisesRegex(EvidenceTransitionError, "from_status is stale"):
            apply_evidence_review(thesis(), stale)

        wrong_version = EvidenceReview(
            review_id="wrong-version",
            thesis_id="sma-20-100-nasdaq-30m",
            thesis_version="2",
            code_hash="b" * 64,
            from_status=EvidenceStatus.DRAFT,
            to_status=EvidenceStatus.REGISTERED,
            reviewer_id="reviewer-bob",
            reviewed_at=NOW + timedelta(hours=1),
            evidence_artifact_hash="d" * 64,
            reason="This review belongs to a different immutable version.",
        )
        with self.assertRaisesRegex(EvidenceTransitionError, "exact thesis version"):
            apply_evidence_review(thesis(), wrong_version)

    def test_material_change_creates_a_new_draft(self) -> None:
        old = thesis(EvidenceStatus.VALIDATED)

        changed = old.supersede(
            thesis_version="2",
            strategy_version="scanner-2",
            code_hash="e" * 64,
            created_at=NOW + timedelta(days=1),
            specification_hash="f" * 64,
        )

        self.assertIs(changed.evidence_status, EvidenceStatus.DRAFT)
        self.assertEqual(changed.supersedes_version, "1")
        self.assertEqual(changed.code_hash, "e" * 64)


class EvidenceAndDeploymentSeparationTests(unittest.TestCase):
    def test_validation_alone_confers_no_deployment_authority(self) -> None:
        decision = assess_deployment(
            thesis(EvidenceStatus.VALIDATED),
            None,
            at=NOW + timedelta(hours=2),
        )

        self.assertFalse(decision.eligible)
        self.assertIn("deployment_grant_missing", decision.reasons)

    def test_active_exactly_scoped_grant_and_validation_are_both_required(self) -> None:
        decision = assess_deployment(
            thesis(EvidenceStatus.VALIDATED),
            active_grant(),
            at=NOW + timedelta(hours=2),
            intent=scoped_intent(),
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.reasons, ())

    def test_unvalidated_evidence_denies_even_with_active_grant(self) -> None:
        decision = assess_deployment(
            thesis(EvidenceStatus.HOLDOUT_PASSED),
            active_grant(),
            at=NOW + timedelta(hours=2),
            intent=scoped_intent(),
        )

        self.assertFalse(decision.eligible)
        self.assertIn("evidence_not_validated", decision.reasons)

    def test_grant_is_bound_to_code_account_environment_and_intent_scope(self) -> None:
        wrong_code = active_grant(code_hash="9" * 64)
        wrong_account_intent = scoped_intent(account_id="some-other-account")

        code_decision = assess_deployment(
            thesis(EvidenceStatus.VALIDATED),
            wrong_code,
            at=NOW + timedelta(hours=2),
        )
        account_decision = assess_deployment(
            thesis(EvidenceStatus.VALIDATED),
            active_grant(),
            at=NOW + timedelta(hours=2),
            intent=wrong_account_intent,
        )

        self.assertIn("deployment_grant_version_mismatch", code_decision.reasons)
        self.assertIn("intent_outside_deployment_scope", account_decision.reasons)

    def test_expired_grant_denies_deployment(self) -> None:
        decision = assess_deployment(
            thesis(EvidenceStatus.VALIDATED),
            active_grant(expires_at=NOW + timedelta(minutes=30)),
            at=NOW + timedelta(hours=2),
        )

        self.assertFalse(decision.eligible)
        self.assertIn("deployment_grant_inactive", decision.reasons)

    def test_grant_type_cannot_cross_environment(self) -> None:
        with self.assertRaisesRegex(ValueError, "scoped to mainnet"):
            active_grant(
                grant_type=GrantType.MANUAL_MAINNET_CANARY,
                environment=Environment.TESTNET,
            )

    def test_float_grant_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "must not be float"):
            active_grant(max_notional=1000.0)


if __name__ == "__main__":
    unittest.main()
