from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import unittest

from trading_harness.errors import ValidationError
from trading_harness.hyperliquid_wire import (
    HyperliquidNetwork,
    PerpInstrumentMetadata,
    build_protected_order_action,
    format_perp_price,
    format_perp_size,
)
from trading_harness.planning import RiskTicketStatus, quote_risk_ticket
from tests.test_planning import NOW, account, assessment, identity, technical


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def protected_plan():
    selected = technical()
    ticket = quote_risk_ticket(
        ticket_id="wire-ticket",
        assessment=assessment(selected),
        technical=selected,
        identity=identity(),
        account=account(),
        at=NOW,
    )
    if ticket.status is not RiskTicketStatus.AWAITING_APPROVAL or ticket.plan is None:
        raise AssertionError("test fixture did not create a protected plan")
    return ticket.plan


def metadata(**changes: object) -> PerpInstrumentMetadata:
    values: dict[str, object] = {
        "symbol": "ETH",
        "asset_id": 1,
        "sz_decimals": 3,
        "max_leverage": Decimal("25"),
        "margin_mode": "cross",
        "is_delisted": False,
        "source_hash": digest("metadata"),
    }
    values.update(changes)
    return PerpInstrumentMetadata(**values)  # type: ignore[arg-type]


def walk(value: object) -> list[object]:
    if isinstance(value, dict):
        result: list[object] = []
        for key, item in value.items():
            result.extend(walk(key))
            result.extend(walk(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(walk(item))
        return result
    return [value]


class HyperliquidTickTests(unittest.TestCase):
    def test_size_is_never_rounded(self) -> None:
        self.assertEqual(format_perp_size(Decimal("1.2300"), sz_decimals=3), "1.23")
        self.assertEqual(format_perp_size(Decimal("0.001"), sz_decimals=3), "0.001")
        with self.assertRaisesRegex(ValidationError, "szDecimals"):
            format_perp_size(Decimal("0.0001"), sz_decimals=3)
        with self.assertRaisesRegex(ValidationError, "positive"):
            format_perp_size(Decimal("0"), sz_decimals=3)
        with self.assertRaises(TypeError):
            format_perp_size(0.1, sz_decimals=3)  # type: ignore[arg-type]

    def test_price_enforces_both_tick_rules_without_rounding(self) -> None:
        self.assertEqual(format_perp_price(Decimal("1234.5"), sz_decimals=4), "1234.5")
        self.assertEqual(format_perp_price(Decimal("123456"), sz_decimals=4), "123456")
        self.assertEqual(format_perp_price(Decimal("0.012345"), sz_decimals=0), "0.012345")
        with self.assertRaisesRegex(ValidationError, "significant"):
            format_perp_price(Decimal("1234.56"), sz_decimals=4)
        with self.assertRaisesRegex(ValidationError, "decimal places"):
            format_perp_price(Decimal("12.345"), sz_decimals=4)


class ProtectedActionTests(unittest.TestCase):
    def test_builds_exact_three_leg_normal_tpsl_action(self) -> None:
        plan = protected_plan()
        first = build_protected_order_action(
            plan,
            metadata(),
            network=HyperliquidNetwork.TESTNET,
            at=NOW,
        )
        second = build_protected_order_action(
            plan,
            metadata(),
            network=HyperliquidNetwork.TESTNET,
            at=NOW,
        )

        self.assertEqual(first.action_hash, second.action_hash)
        self.assertEqual(first.action["type"], "order")
        self.assertEqual(first.action["grouping"], "normalTpsl")
        orders = first.action["orders"]
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders[0]["t"], {"limit": {"tif": "Ioc"}})
        self.assertFalse(orders[0]["r"])
        self.assertEqual(orders[1]["t"]["trigger"]["tpsl"], "sl")
        self.assertEqual(orders[2]["t"]["trigger"]["tpsl"], "tp")
        self.assertTrue(orders[1]["r"])
        self.assertTrue(orders[2]["r"])
        self.assertEqual({order["a"] for order in orders}, {1})
        self.assertEqual(
            {order["c"] for order in orders},
            {
                plan.entry.client_order_id,
                plan.protective_stop.client_order_id,
                plan.take_profit.client_order_id,
            },
        )
        self.assertFalse(any(isinstance(value, float) for value in walk(first.as_dict())))
        self.assertFalse(first.as_dict()["signed"])
        self.assertFalse(first.as_dict()["submitted"])

    def test_network_metadata_expiry_and_leverage_fail_closed(self) -> None:
        plan = protected_plan()
        cases = (
            (
                {"network": HyperliquidNetwork.MAINNET, "at": NOW},
                metadata(),
                "environment",
            ),
            (
                {"network": HyperliquidNetwork.TESTNET, "at": NOW},
                metadata(is_delisted=True),
                "delisted",
            ),
            (
                {
                    "network": HyperliquidNetwork.TESTNET,
                    "at": plan.entry.expires_at,
                },
                metadata(),
                "expired",
            ),
            (
                {"network": HyperliquidNetwork.TESTNET, "at": NOW},
                metadata(max_leverage=Decimal("1")),
                "leverage",
            ),
        )
        for arguments, instrument, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    build_protected_order_action(plan, instrument, **arguments)

    def test_wire_rejects_tick_drift_instead_of_changing_approved_economics(self) -> None:
        selected = replace(
            technical(),
            close=Decimal("2500.12"),
            stop_price=Decimal("2400.12"),
            target_price=Decimal("3000.12"),
        )
        ticket = quote_risk_ticket(
            ticket_id="wire-precision",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(),
            at=NOW,
        )
        self.assertIs(ticket.status, RiskTicketStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(ticket.plan)
        # A fresh metadata regime with zero allowed price decimals makes the
        # already approved values unrepresentable.  Dispatch rejects instead
        # of rounding and changing the approved economics.
        with self.assertRaisesRegex(ValidationError, "decimal places|significant"):
            build_protected_order_action(
                ticket.plan,
                metadata(sz_decimals=6),
                network=HyperliquidNetwork.TESTNET,
                at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
