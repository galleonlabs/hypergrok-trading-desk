from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import unittest

from trading_harness.account_risk import (
    AccountRiskLimits,
    compile_account_risk_snapshot,
)
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, ValidationError
from tests.test_hyperliquid_account import (
    ACCOUNT,
    FixtureTransport,
    fetch,
    valid_clearing,
    valid_meta,
)


def flat_clearing() -> dict[str, object]:
    result = valid_clearing(positions=[])
    for field in ("marginSummary", "crossMarginSummary"):
        summary = result[field]
        summary["totalNtlPos"] = "0"  # type: ignore[index]
        summary["totalMarginUsed"] = "0"  # type: ignore[index]
        summary["totalRawUsd"] = "10200"  # type: ignore[index]
    result["crossMaintenanceMarginUsed"] = "0"
    return result


def snapshot(*, meta: object | None = None):
    result, _ = fetch(
        FixtureTransport(
            meta=valid_meta() if meta is None else meta,
            clearing=flat_clearing(),
            orders=[],
        ),
        network="testnet",
    )
    return result


def limits(**changes: object) -> AccountRiskLimits:
    values: dict[str, object] = {
        "account_id": "testnet-canary",
        "main_account_address": ACCOUNT,
        "environment": Environment.TESTNET,
        "daily_loss_limit": Decimal("100"),
        "aggregate_open_risk_limit": Decimal("75"),
        "max_notional": Decimal("5000"),
        "leverage": Decimal("2"),
    }
    values.update(changes)
    return AccountRiskLimits(**values)  # type: ignore[arg-type]


class AccountRiskCompilerTests(unittest.TestCase):
    def test_compiles_exact_fresh_flat_account_budget(self) -> None:
        result = compile_account_risk_snapshot(
            snapshot(),
            symbol="ETH",
            limits=limits(),
            daily_loss_used=Decimal("10"),
            open_risk_used=Decimal("5"),
        )

        self.assertEqual(result.account_id, "testnet-canary")
        self.assertIs(result.environment, Environment.TESTNET)
        self.assertEqual(result.equity, Decimal("10200"))
        self.assertEqual(result.available_collateral, Decimal("9500"))
        self.assertEqual(result.daily_loss_remaining, Decimal("90"))
        self.assertEqual(result.open_risk_remaining, Decimal("70"))
        self.assertEqual(result.max_notional, Decimal("5000"))
        self.assertEqual(result.lot_size, Decimal("0.0001"))
        self.assertEqual(result.leverage, Decimal("2"))
        self.assertRegex(result.artifact_hash, r"^[0-9a-f]{64}$")

    def test_math_is_independent_of_ambient_decimal_precision(self) -> None:
        arguments = {
            "symbol": "ETH",
            "limits": limits(),
            "daily_loss_used": Decimal("10.123456789"),
            "open_risk_used": Decimal("5.987654321"),
        }
        venue = snapshot()
        with localcontext() as context:
            context.prec = 6
            first = compile_account_risk_snapshot(venue, **arguments)
        with localcontext() as context:
            context.prec = 50
            second = compile_account_risk_snapshot(venue, **arguments)
        self.assertEqual(first, second)

    def test_network_address_exposure_and_open_orders_fail_closed(self) -> None:
        venue = snapshot()
        cases = (
            (limits(environment=Environment.MAINNET), venue, "network"),
            (
                limits(main_account_address="0x" + "2" * 40),
                venue,
                "address",
            ),
        )
        positioned, _ = fetch(FixtureTransport(), network="testnet")
        ordered, _ = fetch(
            FixtureTransport(clearing=flat_clearing()),
            network="testnet",
        )
        cases += (
            (limits(), positioned, "flat account"),
            (limits(), ordered, "flat account"),
        )
        for selected_limits, selected_venue, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(StateConflict, message):
                    compile_account_risk_snapshot(
                        selected_venue,
                        symbol="ETH",
                        limits=selected_limits,
                        daily_loss_used=0,
                        open_risk_used=0,
                    )

    def test_delisted_metadata_and_unsafe_limit_configuration_are_rejected(self) -> None:
        meta = deepcopy(valid_meta())
        meta["universe"][1]["isDelisted"] = True  # type: ignore[index]
        with self.assertRaisesRegex(StateConflict, "delisted"):
            compile_account_risk_snapshot(
                snapshot(meta=meta),
                symbol="ETH",
                limits=limits(),
                daily_loss_used=0,
                open_risk_used=0,
            )
        with self.assertRaisesRegex(ValidationError, "cannot exceed 2"):
            limits(leverage=Decimal("3"))
        with self.assertRaisesRegex(ValidationError, "flat account"):
            limits(require_flat_account=False)


if __name__ == "__main__":
    unittest.main()
