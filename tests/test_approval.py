from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import unittest

from trading_harness.approval import (
    PlanApproval,
    RecoveryAuthorization,
    TestnetApprovalAuthority,
    TestnetRecoveryAuthority,
    verified_execution_approval,
    verified_recovery_permit,
)
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_store import TrustedApproval
from trading_harness.hyperliquid_wire import HyperliquidNetwork
from trading_harness.planning import quote_risk_ticket
from tests.test_planning import NOW, account, assessment, identity, technical
from tests.test_hyperliquid_recovery import (
    RECOVERY_NOW,
    build_close,
    build_noop,
    incident as recovery_incident,
)


def ticket():
    selected = technical()
    return quote_risk_ticket(
        ticket_id="approval-ticket",
        assessment=assessment(selected),
        technical=selected,
        identity=identity(),
        account=account(),
        at=NOW,
    )


class TestnetApprovalTests(unittest.TestCase):
    def authority(self) -> TestnetApprovalAuthority:
        return TestnetApprovalAuthority(
            b"a" * 32,
            key_id="local-test-key-v1",
            audience="testnet-executor",
        )

    def test_exact_terminal_confirmation_issues_and_verifies_redacted_token(self) -> None:
        risk = ticket()
        approval = self.authority().issue(
            risk,
            approval_id="approval-1",
            approver_id="local-user",
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:16]}",
            at=NOW + timedelta(seconds=1),
        )
        token_hash = self.authority().verify(
            approval,
            risk,
            at=NOW + timedelta(seconds=2),
        )

        self.assertEqual(token_hash, approval.token_hash)
        self.assertRegex(token_hash, r"^[0-9a-f]{64}$")
        self.assertTrue(approval.redacted_dict()["mac_redacted"])
        self.assertNotIn(approval.mac, repr(approval.redacted_dict()))
        trusted = verified_execution_approval(
            self.authority(),
            approval,
            risk,
            at=NOW + timedelta(seconds=2),
        )
        self.assertIsInstance(trusted, TrustedApproval)
        self.assertEqual(trusted.token_hash, token_hash)

    def test_chat_like_or_wrong_confirmation_cannot_issue(self) -> None:
        risk = ticket()
        for confirmation in (
            "approve",
            f"approve {risk.ticket_id}",
            f"approve {risk.ticket_id} {'0' * 12}",
        ):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(ValidationError, "confirmation"):
                    self.authority().issue(
                        risk,
                        approval_id="approval-bad",
                        approver_id="local-user",
                        confirmation=confirmation,
                        at=NOW + timedelta(seconds=1),
                    )

    def test_tamper_wrong_audience_ticket_and_expiry_fail(self) -> None:
        risk = ticket()
        authority = self.authority()
        approval = authority.issue(
            risk,
            approval_id="approval-2",
            approver_id="local-user",
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:16]}",
            at=NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(StateConflict, "MAC"):
            authority.verify(
                replace(approval, approver_id="attacker"),
                risk,
                at=NOW + timedelta(seconds=2),
            )
        with self.assertRaisesRegex(StateConflict, "authority"):
            TestnetApprovalAuthority(
                b"a" * 32,
                key_id="local-test-key-v1",
                audience="other-audience",
            ).verify(approval, risk, at=NOW + timedelta(seconds=2))
        with self.assertRaisesRegex(StateConflict, "active"):
            authority.verify(approval, risk, at=approval.expires_at)

    def test_hmac_approval_type_refuses_mainnet_and_short_secrets(self) -> None:
        with self.assertRaisesRegex(ValidationError, "32 bytes"):
            TestnetApprovalAuthority(
                b"short",
                key_id="key",
                audience="audience",
            )
        risk = ticket()
        approval = self.authority().issue(
            risk,
            approval_id="approval-3",
            approver_id="local-user",
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:16]}",
            at=NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValidationError, "testnet-only"):
            PlanApproval(
                approval_id=approval.approval_id,
                ticket_id=approval.ticket_id,
                ticket_hash=approval.ticket_hash,
                plan_hash=approval.plan_hash,
                account_id=approval.account_id,
                environment=Environment.MAINNET,
                audience=approval.audience,
                approver_id=approval.approver_id,
                issued_at=approval.issued_at,
                expires_at=approval.expires_at,
                key_id=approval.key_id,
                mac=approval.mac,
            )


class TestnetRecoveryAuthorityTests(unittest.TestCase):
    POLICY_HASH = hashlib.sha256(b"testnet-recovery-signer-policy").hexdigest()

    def authority(self, *, secret: bytes = b"r" * 32) -> TestnetRecoveryAuthority:
        return TestnetRecoveryAuthority(
            secret,
            key_id="recovery-key-v1",
            issuer_id="isolated-safety-authority",
            audience="testnet-recovery-worker",
        )

    def test_exact_recovery_is_authenticated_and_materialized_as_permit(self) -> None:
        recovery = build_close()
        incident = recovery_incident()
        authorization = self.authority().issue(
            recovery,
            incident,
            permit_id="recovery-permit-1",
            safety_policy_hash=self.POLICY_HASH,
            at=RECOVERY_NOW,
        )
        permit = verified_recovery_permit(
            self.authority(),
            authorization,
            recovery,
            incident,
            safety_policy_hash=self.POLICY_HASH,
            at=RECOVERY_NOW + timedelta(seconds=1),
        )

        self.assertIsInstance(authorization, RecoveryAuthorization)
        self.assertEqual(permit.token_hash, authorization.token_hash)
        self.assertEqual(permit.recovery_hash, recovery.recovery_hash)
        self.assertEqual(permit.recovery_material["close_size"], "0.5")
        self.assertEqual(permit.safety_policy_hash, self.POLICY_HASH)
        self.assertTrue(authorization.redacted_dict()["mac_redacted"])
        self.assertNotIn(authorization.mac, repr(authorization.redacted_dict()))

    def test_tamper_wrong_policy_stale_incident_and_expiry_fail_closed(self) -> None:
        recovery = build_close()
        incident = recovery_incident()
        authority = self.authority()
        authorization = authority.issue(
            recovery,
            incident,
            permit_id="recovery-permit-2",
            safety_policy_hash=self.POLICY_HASH,
            at=RECOVERY_NOW,
        )
        with self.assertRaisesRegex(StateConflict, "MAC"):
            authority.verify(
                replace(authorization, source_hash="0" * 64),
                recovery,
                incident,
                safety_policy_hash=self.POLICY_HASH,
                at=RECOVERY_NOW + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(StateConflict, "exact current evidence"):
            authority.verify(
                authorization,
                recovery,
                incident,
                safety_policy_hash="1" * 64,
                at=RECOVERY_NOW + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(StateConflict, "exact current evidence"):
            authority.verify(
                authorization,
                recovery,
                replace(incident, revision=incident.revision + 1),
                safety_policy_hash=self.POLICY_HASH,
                at=RECOVERY_NOW + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(StateConflict, "not active"):
            authority.verify(
                authorization,
                recovery,
                incident,
                safety_policy_hash=self.POLICY_HASH,
                at=authorization.expires_at,
            )

    def test_noop_binds_original_attempt_and_mainnet_or_mutation_cannot_issue(self) -> None:
        noop = build_noop()
        incident = recovery_incident()
        authorization = self.authority().issue(
            noop,
            incident,
            permit_id="recovery-permit-noop",
            safety_policy_hash=self.POLICY_HASH,
            at=RECOVERY_NOW,
        )
        self.assertEqual(authorization.original_attempt_id, noop.attempt_id)
        self.assertEqual(authorization.original_nonce, noop.original_nonce)
        self.assertEqual(authorization.preflight_hash, noop.preflight_hash)

        with self.assertRaisesRegex(StateConflict, "testnet-only"):
            self.authority().issue(
                replace(build_close(), network=HyperliquidNetwork.MAINNET),
                incident,
                permit_id="mainnet-forbidden",
                safety_policy_hash=self.POLICY_HASH,
                at=RECOVERY_NOW,
            )
        with self.assertRaisesRegex(ValidationError, "recovery hash"):
            self.authority().issue(
                replace(build_close(), close_size=build_close().close_size / 2),
                incident,
                permit_id="mutated-recovery",
                safety_policy_hash=self.POLICY_HASH,
                at=RECOVERY_NOW,
            )

    def test_recovery_authority_rejects_short_secret(self) -> None:
        with self.assertRaisesRegex(ValidationError, "32 bytes"):
            self.authority(secret=b"short")


if __name__ == "__main__":
    unittest.main()
