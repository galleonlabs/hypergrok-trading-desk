from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import unittest

from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from trading_harness.execution_grant import (
    SignedInfrastructureGrant,
    TestnetInfrastructureGrantAuthority,
    TrustedInfrastructureGrant,
    infrastructure_grant_confirmation,
    signed_infrastructure_grant_from_dict,
)


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
POLICY = hashlib.sha256(b"learning-risk-policy").hexdigest()


class InfrastructureGrantTests(unittest.TestCase):
    def authority(self) -> TestnetInfrastructureGrantAuthority:
        return TestnetInfrastructureGrantAuthority(
            b"g" * 32,
            issuer_id="local-learning-authority",
            key_id="learning-key-v1",
            audience="isolated-testnet-executor",
        )

    def issue(self) -> SignedInfrastructureGrant:
        return self.authority().issue(
            grant_id="learning-grant-1",
            generation=1,
            account_id="learning-account",
            allowed_instruments=("ETH-PERP",),
            risk_policy_hash=POLICY,
            max_loss="5",
            max_notional="100",
            max_leverage="2",
            confirmation=infrastructure_grant_confirmation(
                grant_id="learning-grant-1",
                generation=1,
                account_id="learning-account",
                allowed_instruments=("ETH-PERP",),
                risk_policy_hash=POLICY,
                max_loss="5",
                max_notional="100",
                max_leverage="2",
                ttl_seconds=3_600,
            ),
            at=NOW,
        )

    def test_exact_local_confirmation_issues_non_profitability_testnet_grant(self) -> None:
        signed = self.issue()
        trusted = self.authority().verify(signed, at=NOW + timedelta(seconds=1))

        self.assertIsInstance(trusted, TrustedInfrastructureGrant)
        self.assertIs(trusted.environment, Environment.TESTNET)
        self.assertEqual(("ETH-PERP",), trusted.allowed_instruments)
        self.assertEqual(Decimal("5"), trusted.max_loss)
        self.assertFalse(signed.payload()["profitability_qualified"])
        self.assertFalse(signed.payload()["mainnet_authorized"])
        self.assertRegex(trusted.grant_hash, r"^[0-9a-f]{64}$")

    def test_tamper_expiry_wrong_authority_and_bad_confirmation_fail(self) -> None:
        signed = self.issue()
        with self.assertRaisesRegex(StateConflict, "MAC"):
            self.authority().verify(
                replace(signed, max_notional=Decimal("101")),
                at=NOW + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(StateConflict, "active"):
            self.authority().verify(signed, at=signed.expires_at)
        with self.assertRaisesRegex(StateConflict, "authority"):
            TestnetInfrastructureGrantAuthority(
                b"g" * 32,
                issuer_id="other",
                key_id="learning-key-v1",
                audience="isolated-testnet-executor",
            ).verify(signed, at=NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ValidationError, "confirmation"):
            self.authority().issue(
                grant_id="learning-grant-2",
                generation=1,
                account_id="learning-account",
                allowed_instruments=("ETH-PERP",),
                risk_policy_hash=POLICY,
                max_loss="5",
                max_notional="100",
                max_leverage="2",
                confirmation="approve",
                at=NOW,
            )

    def test_mainnet_excess_leverage_float_and_long_lifetime_are_impossible(self) -> None:
        base = self.issue()
        with self.assertRaisesRegex(ValidationError, "testnet-only"):
            replace(base, environment=Environment.MAINNET)
        with self.assertRaisesRegex(ValidationError, "2x"):
            replace(base, max_leverage=Decimal("2.1"))
        with self.assertRaises((TypeError, ValidationError)):
            replace(base, max_loss=1.5)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValidationError, "24-hour"):
            replace(base, expires_at=base.issued_at + timedelta(hours=25))

    def test_secret_and_grant_fields_are_strict(self) -> None:
        with self.assertRaisesRegex(ValidationError, "32 bytes"):
            TestnetInfrastructureGrantAuthority(
                b"short",
                issuer_id="issuer",
                key_id="key",
                audience="audience",
            )
        with self.assertRaisesRegex(ValidationError, "allowed_instruments"):
            replace(self.issue(), allowed_instruments=())

    def test_portable_signed_artifact_round_trips_before_authentication(self) -> None:
        signed = self.issue()

        parsed = signed_infrastructure_grant_from_dict(signed.as_dict())
        trusted = self.authority().verify(parsed, at=NOW + timedelta(seconds=1))

        self.assertEqual(signed, parsed)
        self.assertEqual(signed.grant_hash, trusted.grant_hash)
        self.assertFalse(parsed.as_dict()["mainnet_authorized"])

    def test_portable_artifact_rejects_field_hash_and_authority_tampering(self) -> None:
        artifact = self.issue().as_dict()
        cases = (
            {**artifact, "max_notional": "101"},
            {**artifact, "grant_hash": "f" * 64},
            {**artifact, "mainnet_authorized": True},
            {**artifact, "extra": "unsupported"},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    signed_infrastructure_grant_from_dict(value)

    def test_confirmation_changes_for_every_material_scope_change(self) -> None:
        base = {
            "grant_id": "learning-grant-1",
            "generation": 1,
            "account_id": "learning-account",
            "allowed_instruments": ("ETH-PERP",),
            "risk_policy_hash": POLICY,
            "max_loss": "5",
            "max_notional": "100",
            "max_leverage": "2",
            "ttl_seconds": 3_600,
        }
        expected = infrastructure_grant_confirmation(**base)
        changes = {
            "generation": 2,
            "allowed_instruments": ("BTC-PERP",),
            "max_loss": "6",
            "max_notional": "101",
            "max_leverage": "1",
            "ttl_seconds": 7_200,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    expected,
                    infrastructure_grant_confirmation(
                        **{**base, field: value}
                    ),
                )


if __name__ == "__main__":
    unittest.main()
