from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import unittest

from trading_harness.approval import (
    PlanApproval,
    TestnetApprovalAuthority,
    verified_execution_approval,
)
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_store import TrustedApproval
from trading_harness.planning import quote_risk_ticket
from tests.test_planning import NOW, account, assessment, identity, technical


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
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:12]}",
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
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:12]}",
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
            confirmation=f"approve {risk.ticket_id} {risk.ticket_hash[:12]}",
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


if __name__ == "__main__":
    unittest.main()
