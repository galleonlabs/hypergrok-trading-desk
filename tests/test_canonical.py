from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from trading_harness.canonical import (
    CanonicalizationError,
    SEMANTIC_INTENT_HASH_DOMAIN,
    canonical_decimal,
    canonical_json,
    domain_hash,
    semantic_intent_hash,
)
from trading_harness.domain import (
    Authorization,
    AuthorizationState,
    Environment,
    OrderType,
    SemanticIntent,
    Side,
)


NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)


def sample_intent(**changes: object) -> SemanticIntent:
    values: dict[str, object] = {
        "intent_id": "intent-001",
        "thesis_id": "sma-cross",
        "thesis_version": "3",
        "strategy_version": "compiler-2",
        "code_hash": "a" * 64,
        "venue": "hyperliquid",
        "account_id": "account-canary",
        "environment": Environment.TESTNET,
        "instrument": "ETH-PERP",
        "action": "open",
        "side": Side.BUY,
        "quantity": Decimal("0.5100"),
        "order_type": OrderType.LIMIT,
        "expires_at": NOW + timedelta(minutes=2),
        "client_order_id": "01J5INTENT001",
        "limit_price": Decimal("3000.00"),
        "price_bound": Decimal("3015"),
        "stop_price": Decimal("2900.0"),
        "max_slippage_bps": Decimal("50.00"),
        "fee_bps": Decimal("3.5"),
        "time_in_force": "GTC",
        "signal_instance_hash": "b" * 64,
    }
    values.update(changes)
    return SemanticIntent(**values)


class CanonicalDecimalTests(unittest.TestCase):
    def test_equivalent_decimals_have_one_non_exponent_representation(self) -> None:
        self.assertEqual(canonical_decimal(Decimal("1.2300")), "1.23")
        self.assertEqual(canonical_decimal(Decimal("1.23")), "1.23")
        self.assertEqual(canonical_decimal(Decimal("1E+3")), "1000")
        self.assertEqual(canonical_decimal(Decimal("-0E-20")), "0")

    def test_non_finite_and_non_decimal_values_are_rejected(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalizationError):
                    canonical_decimal(value)
        with self.assertRaises(TypeError):
            canonical_decimal(1.25)  # type: ignore[arg-type]

    def test_pathological_exponents_are_rejected_before_plain_expansion(self) -> None:
        for value in (Decimal("1E+1000000"), Decimal("1E-1000000")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    CanonicalizationError, "outside supported bounds"
                ):
                    canonical_decimal(value)


class SemanticIntentCanonicalizationTests(unittest.TestCase):
    def test_exact_decimals_are_json_strings(self) -> None:
        encoded = json.loads(canonical_json(sample_intent()))

        self.assertEqual(encoded["quantity"], "0.51")
        self.assertEqual(encoded["limit_price"], "3000")
        self.assertEqual(encoded["max_slippage_bps"], "50")
        self.assertIsInstance(encoded["quantity"], str)
        self.assertEqual(encoded["expires_at"], "2026-08-21T18:02:00.000000Z")

    def test_equivalent_exact_inputs_produce_the_same_digest(self) -> None:
        first = sample_intent(quantity=Decimal("0.5100"), limit_price="3000.00")
        second = sample_intent(quantity="0.51", limit_price=Decimal("3E+3"))

        self.assertEqual(semantic_intent_hash(first), semantic_intent_hash(second))

    def test_hash_is_domain_separated_and_covers_economic_fields(self) -> None:
        intent = sample_intent()
        digest = semantic_intent_hash(intent)

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            "9163e725dafcebd8742dc68795d34dda21eaf9ed1ae5a9d6a31defd2727e2358",
        )
        self.assertNotEqual(digest, domain_hash("some-other-protocol/v1", intent))
        self.assertNotEqual(
            digest,
            semantic_intent_hash(replace(intent, price_bound=Decimal("3016"))),
        )
        self.assertEqual(
            digest,
            domain_hash(SEMANTIC_INTENT_HASH_DOMAIN, intent),
        )

    def test_float_monetary_inputs_are_rejected_before_hashing(self) -> None:
        for field_name, value in (
            ("quantity", 0.51),
            ("limit_price", 3000.0),
            ("price_bound", 3015.0),
            ("stop_price", 2900.0),
            ("leverage", 2.0),
            ("max_slippage_bps", 50.0),
            ("fee_bps", 3.5),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(TypeError, "must not be float"):
                    sample_intent(**{field_name: value})

    def test_mapping_parser_accepts_only_exact_strings_for_money(self) -> None:
        mapping = {
            "intent_id": "intent-002",
            "thesis_id": "sma-cross",
            "thesis_version": "3",
            "strategy_version": "compiler-2",
            "code_hash": "a" * 64,
            "venue": "hyperliquid",
            "account_id": "account-canary",
            "environment": "testnet",
            "instrument": "BTC-PERP",
            "action": "open",
            "side": "sell",
            "quantity": "0.125",
            "order_type": "market",
            "expires_at": "2026-08-21T18:02:00Z",
            "client_order_id": "01J5INTENT002",
            "price_bound": "59000.25",
        }

        intent = SemanticIntent.from_mapping(mapping)

        self.assertIs(intent.environment, Environment.TESTNET)
        self.assertIs(intent.side, Side.SELL)
        self.assertEqual(intent.quantity, Decimal("0.125"))
        self.assertEqual(intent.price_bound, Decimal("59000.25"))

    def test_intent_is_frozen_and_naive_expiry_is_rejected(self) -> None:
        intent = sample_intent()
        with self.assertRaises(FrozenInstanceError):
            intent.quantity = Decimal("1")  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            sample_intent(expires_at=datetime(2026, 8, 21, 18, 2))

    def test_signal_instance_hash_must_be_a_sha256_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            sample_intent(signal_instance_hash="signal-001")


class AuthorizationTests(unittest.TestCase):
    def test_authorization_is_bound_to_one_hash_account_and_environment(self) -> None:
        intent = sample_intent()
        authorization = Authorization(
            authorization_id="authorization-001",
            intent_hash=semantic_intent_hash(intent),
            grant_id="grant-001",
            account_id=intent.account_id,
            environment=intent.environment,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            audience="admission-service",
            approver_ids=("risk-owner", "desk-owner"),
        )

        self.assertTrue(authorization.is_active(NOW + timedelta(seconds=30)))
        self.assertFalse(authorization.is_active(NOW + timedelta(minutes=1)))
        self.assertNotEqual(
            authorization.intent_hash,
            semantic_intent_hash(replace(intent, quantity=Decimal("0.52"))),
        )
        self.assertIs(
            authorization.with_state(AuthorizationState.CONSUMING).state,
            AuthorizationState.CONSUMING,
        )

    def test_authorization_rejects_invalid_hash_or_interval(self) -> None:
        values: dict[str, object] = {
            "authorization_id": "authorization-001",
            "intent_hash": "a" * 64,
            "grant_id": "grant-001",
            "account_id": "account-canary",
            "environment": Environment.TESTNET,
            "issued_at": NOW,
            "expires_at": NOW + timedelta(minutes=1),
            "audience": "admission-service",
        }
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            Authorization(**{**values, "intent_hash": "not-a-digest"})
        with self.assertRaisesRegex(ValueError, "must be after"):
            Authorization(**{**values, "expires_at": NOW})


if __name__ == "__main__":
    unittest.main()
