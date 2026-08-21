from __future__ import annotations

from decimal import Decimal
import unittest

from trading_harness.errors import PolicyViolation, ValidationError
from trading_harness.policy import (
    AccountExposure,
    ExposureQuote,
    PlatformCeilings,
    RiskPolicy,
)


def policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "risk-canary",
        "version": "1",
        "max_order_quantity": Decimal("10"),
        "max_order_notional": Decimal("10000"),
        "max_order_worst_case_loss": Decimal("1000"),
        "max_account_gross_notional": Decimal("20000"),
        "max_account_worst_case_loss": Decimal("2000"),
        "max_leverage": Decimal("3"),
        "max_slippage_bps": Decimal("100"),
        "max_fee_bps": Decimal("20"),
        "allowed_instruments": ("ETH-PERP",),
        "allowed_actions": ("simulate_order",),
        "allowed_order_types": ("limit",),
    }
    values.update(changes)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def quote(**changes: object) -> ExposureQuote:
    values: dict[str, object] = {
        "intent_hash": "a" * 64,
        "quantity": Decimal("1"),
        "notional": Decimal("3000"),
        "worst_case_loss": Decimal("125"),
        "slippage_bps": Decimal("50"),
        "fee_bps": Decimal("4"),
    }
    values.update(changes)
    return ExposureQuote(**values)  # type: ignore[arg-type]


class ExactDecimalPolicyTests(unittest.TestCase):
    def test_float_is_rejected_at_policy_boundary(self) -> None:
        with self.assertRaises(ValidationError):
            policy(max_order_notional=10_000.0)
        with self.assertRaises(ValidationError):
            quote(notional=3_000.0)

    def test_policy_serialization_round_trip_preserves_exact_values(self) -> None:
        original = policy(max_order_notional=Decimal("10000.000"))
        restored = RiskPolicy.from_dict(original.to_dict())

        self.assertEqual(restored, original)
        self.assertIsInstance(restored.max_order_notional, Decimal)


class CeilingEnforcementTests(unittest.TestCase):
    def test_caller_cannot_widen_compiled_platform_ceiling(self) -> None:
        lax_injected_ceiling = PlatformCeilings(
            version="attempted-override",
            max_order_quantity=Decimal("999999999"),
            max_order_notional=Decimal("999999999"),
            max_order_worst_case_loss=Decimal("999999999"),
            max_account_gross_notional=Decimal("9999999999"),
            max_account_worst_case_loss=Decimal("9999999999"),
            max_leverage=Decimal("999"),
            max_slippage_bps=Decimal("9999"),
            max_fee_bps=Decimal("9999"),
        )
        widened_policy = policy(
            max_order_notional=Decimal("2000000"),
            max_account_gross_notional=Decimal("3000000"),
        )

        with self.assertRaisesRegex(PolicyViolation, "PLATFORM_CEILING_EXCEEDED"):
            widened_policy.validate_ceiling(lax_injected_ceiling)

    def test_account_limit_counts_booked_and_reserved_exposure(self) -> None:
        current = AccountExposure(
            reserved_notional=Decimal("8000"),
            reserved_loss=Decimal("700"),
            booked_notional=Decimal("10000"),
            booked_loss=Decimal("1100"),
        )

        with self.assertRaisesRegex(PolicyViolation, "ACCOUNT_NOTIONAL_LIMIT"):
            policy().validate_order(
                instrument="ETH-PERP",
                action="simulate_order",
                order_type="limit",
                leverage=Decimal("1"),
                quote=quote(),
                current=current,
            )

    def test_scope_and_order_limits_fail_closed(self) -> None:
        cases = (
            ("instrument", {"instrument": "BTC-PERP"}, "INSTRUMENT_NOT_ALLOWED"),
            ("action", {"action": "withdraw"}, "PLATFORM_ACTION_NOT_ALLOWED"),
            ("type", {"order_type": "market"}, "ORDER_TYPE_NOT_ALLOWED"),
            (
                "loss",
                {"quote": quote(worst_case_loss=Decimal("1000.01"))},
                "ORDER_LOSS_LIMIT",
            ),
        )
        base = {
            "instrument": "ETH-PERP",
            "action": "simulate_order",
            "order_type": "limit",
            "leverage": Decimal("1"),
            "quote": quote(),
            "current": AccountExposure(),
        }
        for label, change, code in cases:
            with self.subTest(label=label):
                arguments = {**base, **change}
                with self.assertRaisesRegex(PolicyViolation, code):
                    policy().validate_order(**arguments)  # type: ignore[arg-type]

    def test_platform_action_allowlist_cannot_be_widened_by_wildcards(self) -> None:
        wildcard_policy = policy(
            allowed_instruments=("*",),
            allowed_actions=("*",),
            allowed_order_types=("*",),
        )
        for action in ("withdraw", "transfer", "place_order", "cancel_all"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    PolicyViolation, "PLATFORM_ACTION_NOT_ALLOWED"
                ):
                    wildcard_policy.validate_order(
                        instrument="ANY",
                        action=action,
                        order_type="anything",
                        leverage=Decimal("1"),
                        quote=quote(),
                        current=AccountExposure(),
                    )

    def test_scope_fields_reject_scalar_strings_and_non_strings(self) -> None:
        for field, value in (
            ("allowed_instruments", "*"),
            ("allowed_actions", "*"),
            ("allowed_order_types", "limit"),
            ("allowed_actions", (1,)),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    policy(**{field: value})

    def test_from_dict_does_not_turn_scalar_wildcard_into_scope(self) -> None:
        encoded = policy().to_dict()
        encoded["allowed_actions"] = "*"

        with self.assertRaises(ValidationError):
            RiskPolicy.from_dict(encoded)


if __name__ == "__main__":
    unittest.main()
