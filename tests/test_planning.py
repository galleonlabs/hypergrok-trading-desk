from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import unittest

from trading_harness.analysis import TechnicalBias, TechnicalSnapshot
from trading_harness.assessment import (
    ProfitabilityGate,
    ProfitabilityStatus,
    build_opportunity_assessment,
)
from trading_harness.domain import Environment, Side
from trading_harness.errors import ValidationError
from trading_harness.execution_grant import TrustedInfrastructureGrant
from trading_harness.planning import (
    AccountRiskSnapshot,
    PlanIdentity,
    ProtectedTradePlan,
    RiskSizingPolicy,
    RiskTicketStatus,
    protected_trade_plan_from_dict,
    quote_risk_ticket,
    quote_infrastructure_learning_ticket,
    risk_ticket_from_dict,
)
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
)
from trading_harness.registered_decision import build_registered_assessment
from trading_harness.strategy import CANDIDATE_V0
from tests.test_registered_decision import (
    AT as REGISTERED_AT,
    SIGNAL as REGISTERED_SIGNAL,
    attestation as registered_attestation,
    sentiment as registered_sentiment,
)


NOW = datetime(2026, 8, 24, 16, 5, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def technical(side: Side = Side.BUY, *, target: str | None = None) -> TechnicalSnapshot:
    buying = side is Side.BUY
    return TechnicalSnapshot(
        symbol="ETH",
        interval="4h",
        as_of=NOW,
        candle_close_time=NOW - timedelta(minutes=5),
        config_version="strategy-v1",
        config_hash=digest("config"),
        data_hash=digest("data"),
        completed_candles=1000,
        ignored_incomplete_candles=0,
        close=Decimal("2500"),
        ema_fast=Decimal("2550" if buying else "2450"),
        ema_slow=Decimal("2500"),
        ema_trend=Decimal("2400" if buying else "2600"),
        rsi=Decimal("60" if buying else "40"),
        atr=Decimal("50"),
        bias=TechnicalBias.BUY if buying else TechnicalBias.SELL,
        stop_price=Decimal("2400" if buying else "2600"),
        target_price=Decimal(target or ("3000" if buying else "2000")),
        reasons=("test",),
    )


def sentiment_snapshot():
    evidence = []
    for index in range(4):
        evidence.append(
            SentimentEvidence(
                evidence_id=f"e-{index}",
                post_id=f"p-{index}",
                source_url=f"https://x.com/example/status/{index}",
                author_hash=digest(f"a-{index}"),
                content_hash=digest(f"c-{index}"),
                cluster_hash=digest(f"k-{index}"),
                published_at=NOW - timedelta(hours=1),
                observed_at=NOW - timedelta(minutes=1),
                polarity=Decimal("0"),
            )
        )
    return build_sentiment_snapshot(
        asset_id="ETH-PERP",
        query="$ETH",
        query_version="q1",
        classifier_version="classifier-v1",
        method=CollectionMethod.X_API,
        window_start=NOW - timedelta(hours=4),
        window_end=NOW - timedelta(minutes=2),
        collected_at=NOW,
        evidence=evidence,
        excluded_count=0,
        collection_complete=True,
        policy=SentimentPolicy(
            version="p1",
            minimum_posts=4,
            minimum_authors=4,
            trim_fraction=Decimal("0"),
            bullish_threshold=Decimal("0.15"),
            bearish_threshold=Decimal("-0.15"),
            max_cluster_share=Decimal("0.5"),
            ttl_seconds=900,
        ),
    )


def assessment(selected: TechnicalSnapshot):
    gate = ProfitabilityGate(
        gate_id="gate-1",
        asset_id="ETH-PERP",
        thesis_id="trend-breakout",
        thesis_version="1",
        strategy_version="strategy-v1",
        artifact_hash=digest("validation"),
        status=ProfitabilityStatus.QUALIFIED,
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        oos_trades=120,
        shadow_closed_signals=55,
        net_expectancy_r=Decimal("0.15"),
        lower_confidence_bound_r=Decimal("0.02"),
    )
    return build_opportunity_assessment(
        assessment_id="assessment-1",
        asset_id="ETH-PERP",
        technical=selected,
        sentiment=sentiment_snapshot(),
        profitability=gate,
        at=NOW,
    )


def account(**changes: object) -> AccountRiskSnapshot:
    values: dict[str, object] = {
        "account_id": "testnet-account",
        "environment": Environment.TESTNET,
        "observed_at": NOW - timedelta(seconds=1),
        "received_at": NOW,
        "equity": Decimal("10000"),
        "available_collateral": Decimal("9000"),
        "daily_loss_remaining": Decimal("100"),
        "open_risk_remaining": Decimal("100"),
        "max_notional": Decimal("1000"),
        "lot_size": Decimal("0.001"),
        "leverage": Decimal("2"),
        "artifact_hash": digest("account"),
    }
    values.update(changes)
    return AccountRiskSnapshot(**values)  # type: ignore[arg-type]


def identity() -> PlanIdentity:
    return PlanIdentity(
        thesis_id="trend-breakout",
        thesis_version="1",
        strategy_version="strategy-v1",
        venue="hyperliquid",
        account_id="testnet-account",
        environment=Environment.TESTNET,
        instrument="ETH-PERP",
    )


class RiskPlanningTests(unittest.TestCase):
    def test_infrastructure_learning_can_quote_without_profitability_claim(self) -> None:
        registered = build_registered_assessment(
            assessment_id="learning-risk",
            asset_id="ETH-PERP",
            signal=REGISTERED_SIGNAL,
            sentiment=registered_sentiment(),
            profitability=None,
            at=REGISTERED_AT,
        )
        self.assertFalse(registered.eligible_for_risk_quote)
        policy = RiskSizingPolicy(
            version="learning-test-mechanics-v1",
            entry_slippage_bps=Decimal("0"),
            exit_slippage_bps=Decimal("0"),
            stop_gap_bps=Decimal("0"),
            round_trip_fee_bps=Decimal("0"),
        )
        grant = TrustedInfrastructureGrant(
            grant_hash=digest("learning-grant"),
            grant_id="learning-grant",
            generation=1,
            account_id="testnet-account",
            environment=Environment.TESTNET,
            allowed_instruments=("ETH-PERP",),
            risk_policy_hash=policy.policy_hash,
            max_loss=Decimal("25"),
            max_notional=Decimal("1000"),
            max_leverage=Decimal("2"),
            issuer_id="test-authority",
            audience="test-executor",
            issued_at=REGISTERED_AT - timedelta(seconds=1),
            not_before=REGISTERED_AT - timedelta(seconds=1),
            expires_at=REGISTERED_AT + timedelta(hours=1),
        )
        ticket = quote_infrastructure_learning_ticket(
            ticket_id="learning-ticket",
            assessment=registered,
            identity=PlanIdentity(
                thesis_id="candidate-v0",
                thesis_version="1",
                strategy_version="1",
                venue="hyperliquid",
                account_id="testnet-account",
                environment=Environment.TESTNET,
                instrument="ETH-PERP",
            ),
            account=account(
                observed_at=REGISTERED_AT - timedelta(seconds=1),
                received_at=REGISTERED_AT,
                max_notional=Decimal("1000"),
            ),
            grant=grant,
            at=REGISTERED_AT,
            policy=policy,
        )

        self.assertIs(ticket.status, RiskTicketStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(ticket.plan)
        self.assertFalse(registered.eligible_to_trade)
        self.assertTrue(ticket.plan.protective_stop.reduce_only)
        self.assertEqual("normalTpsl", ticket.plan.grouping.value)

    def test_long_ticket_is_plan_bound_stopped_and_awaiting_approval(self) -> None:
        selected = technical()
        result = quote_risk_ticket(
            ticket_id="ticket-1",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(),
            at=NOW,
        )

        self.assertIs(result.status, RiskTicketStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(result.plan)
        self.assertGreater(result.quantity, 0)
        self.assertLessEqual(result.stressed_loss, result.risk_budget)
        self.assertGreaterEqual(result.net_reward_risk, 2)
        self.assertIs(result.plan.entry.side, Side.BUY)
        self.assertIs(result.plan.protective_stop.side, Side.SELL)
        self.assertTrue(result.plan.protective_stop.reduce_only)
        self.assertTrue(result.plan.take_profit.reduce_only)
        self.assertEqual(result.plan.entry.quantity, result.plan.protective_stop.quantity)
        self.assertEqual(result.plan.entry.quantity, result.plan.take_profit.quantity)
        self.assertTrue(all(
            value.startswith("0x") and len(value) == 34
            for value in (
                result.plan.entry.client_order_id,
                result.plan.protective_stop.client_order_id,
                result.plan.take_profit.client_order_id,
            )
        ))
        self.assertFalse(result.as_dict()["approval_created"])
        self.assertFalse(result.as_dict()["eligible_to_trade"])

    def test_short_ticket_is_symmetric_and_deterministic(self) -> None:
        selected = technical(Side.SELL)
        arguments = {
            "ticket_id": "ticket-short",
            "assessment": assessment(selected),
            "technical": selected,
            "identity": identity(),
            "account": account(),
            "at": NOW,
        }
        first = quote_risk_ticket(**arguments)
        second = quote_risk_ticket(**arguments)

        self.assertIs(first.status, RiskTicketStatus.AWAITING_APPROVAL)
        self.assertIs(first.plan.entry.side, Side.SELL)
        self.assertIs(first.plan.protective_stop.side, Side.BUY)
        self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)
        self.assertEqual(first.ticket_hash, second.ticket_hash)

    def test_stale_account_or_weak_net_rr_returns_structured_denial(self) -> None:
        selected = technical(target="2700")
        weak = quote_risk_ticket(
            ticket_id="ticket-weak",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(),
            at=NOW,
        )
        stale = quote_risk_ticket(
            ticket_id="ticket-stale",
            assessment=assessment(technical()),
            technical=technical(),
            identity=identity(),
            account=account(
                observed_at=NOW - timedelta(minutes=1),
                received_at=NOW - timedelta(minutes=1),
            ),
            at=NOW,
        )

        self.assertIs(weak.status, RiskTicketStatus.DENIED)
        self.assertIn("net_reward_risk_below_minimum", weak.reason_codes)
        self.assertIsNone(weak.plan)
        self.assertEqual(weak.quantity, 0)
        self.assertIs(stale.status, RiskTicketStatus.DENIED)
        self.assertIn("account_snapshot_stale", stale.reason_codes)

    def test_minimum_lot_and_leverage_fail_closed(self) -> None:
        selected = technical()
        too_coarse = quote_risk_ticket(
            ticket_id="ticket-lot",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(lot_size=Decimal("1")),
            at=NOW,
        )
        excessive_leverage = quote_risk_ticket(
            ticket_id="ticket-leverage",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(leverage=Decimal("3")),
            at=NOW,
        )

        self.assertIn("quantity_below_lot", too_coarse.reason_codes)
        self.assertIn("leverage_limit", excessive_leverage.reason_codes)

    def test_plan_and_ticket_hashes_detect_tampering(self) -> None:
        selected = technical()
        ticket = quote_risk_ticket(
            ticket_id="ticket-tamper",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(),
            at=NOW,
        )
        with self.assertRaisesRegex(ValidationError, "oppose"):
            ProtectedTradePlan(
                assessment_hash=ticket.plan.assessment_hash,
                entry=ticket.plan.entry,
                protective_stop=replace(ticket.plan.protective_stop, side=Side.BUY),
                take_profit=ticket.plan.take_profit,
                grouping=ticket.plan.grouping,
                plan_hash=ticket.plan.plan_hash,
            )
        with self.assertRaisesRegex(ValidationError, "ticket_hash"):
            replace(ticket, quantity=ticket.quantity + Decimal("0.001"))

    def test_risk_math_ignores_ambient_decimal_context(self) -> None:
        selected = technical()
        arguments = {
            "ticket_id": "ticket-context",
            "assessment": assessment(selected),
            "technical": selected,
            "identity": identity(),
            "account": account(),
            "at": NOW,
        }
        with localcontext() as context:
            context.prec = 6
            first = quote_risk_ticket(**arguments).as_dict()
        with localcontext() as context:
            context.prec = 50
            second = quote_risk_ticket(**arguments).as_dict()

        self.assertEqual(first, second)

    def test_initial_policy_caps_risk_and_leverage(self) -> None:
        with self.assertRaisesRegex(ValidationError, "0.25%"):
            RiskSizingPolicy(risk_fraction=Decimal("0.01"))
        with self.assertRaisesRegex(ValidationError, "exceed 2"):
            RiskSizingPolicy(max_leverage=Decimal("3"))

    def test_registered_strategy_path_binds_signal_hash_and_stop(self) -> None:
        registered = build_registered_assessment(
            assessment_id="registered-risk",
            asset_id="ETH-PERP",
            signal=REGISTERED_SIGNAL,
            sentiment=registered_sentiment(),
            profitability=registered_attestation(),
            at=REGISTERED_AT,
        )
        registered_account = account(
            observed_at=REGISTERED_AT - timedelta(seconds=1),
            received_at=REGISTERED_AT,
            max_notional=Decimal("1000"),
        )
        registered_identity = PlanIdentity(
            thesis_id="candidate-v0",
            thesis_version="1",
            strategy_version="1",
            venue="hyperliquid",
            account_id="testnet-account",
            environment=Environment.TESTNET,
            instrument="ETH-PERP",
        )
        no_cost_test_policy = RiskSizingPolicy(
            version="registered-test-mechanics-v1",
            entry_slippage_bps=Decimal("0"),
            exit_slippage_bps=Decimal("0"),
            stop_gap_bps=Decimal("0"),
            round_trip_fee_bps=Decimal("0"),
        )
        ticket = quote_risk_ticket(
            ticket_id="registered-ticket",
            assessment=registered,
            technical=None,
            identity=registered_identity,
            account=registered_account,
            at=REGISTERED_AT,
            policy=no_cost_test_policy,
            strategy=CANDIDATE_V0,
        )

        self.assertIs(ticket.status, RiskTicketStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(ticket.plan)
        self.assertEqual(ticket.plan.entry.signal_instance_hash, registered.signal_hash)
        self.assertEqual(ticket.plan.protective_stop.signal_instance_hash, registered.signal_hash)
        self.assertEqual(ticket.plan.entry.code_hash, CANDIDATE_V0.registration_hash)
        self.assertTrue(ticket.plan.protective_stop.reduce_only)
        self.assertIsNotNone(ticket.plan.protective_stop.stop_price)

    def test_registered_candidate_is_denied_when_stressed_net_rr_is_below_policy(self) -> None:
        registered = build_registered_assessment(
            assessment_id="registered-stress",
            asset_id="ETH-PERP",
            signal=REGISTERED_SIGNAL,
            sentiment=registered_sentiment(),
            profitability=registered_attestation(),
            at=REGISTERED_AT,
        )
        ticket = quote_risk_ticket(
            ticket_id="registered-stress-ticket",
            assessment=registered,
            technical=None,
            identity=PlanIdentity(
                thesis_id="candidate-v0",
                thesis_version="1",
                strategy_version="1",
                venue="hyperliquid",
                account_id="testnet-account",
                environment=Environment.TESTNET,
                instrument="ETH-PERP",
            ),
            account=account(
                observed_at=REGISTERED_AT - timedelta(seconds=1),
                received_at=REGISTERED_AT,
            ),
            at=REGISTERED_AT,
            strategy=CANDIDATE_V0,
        )

        self.assertIs(ticket.status, RiskTicketStatus.DENIED)
        self.assertIn("net_reward_risk_below_minimum", ticket.reason_codes)
        self.assertIsNone(ticket.plan)

    def test_ticket_and_plan_documents_round_trip_and_reject_tampering(self) -> None:
        selected = technical()
        original = quote_risk_ticket(
            ticket_id="ticket-round-trip",
            assessment=assessment(selected),
            technical=selected,
            identity=identity(),
            account=account(),
            at=NOW,
        )
        document = original.as_dict()

        self.assertEqual(risk_ticket_from_dict(document), original)
        self.assertEqual(
            protected_trade_plan_from_dict(document["plan"]),
            original.plan,
        )

        authority = deepcopy(document)
        authority["eligible_to_trade"] = True
        bad_leg = deepcopy(document)
        bad_leg["plan"]["stop_hash"] = "f" * 64
        bad_expiry = deepcopy(document)
        bad_expiry["expires_at"] = "2099-01-01T00:00:00.000Z"
        for tampered in (authority, bad_leg, bad_expiry):
            with self.subTest(tampered=tampered):
                with self.assertRaises(ValidationError):
                    risk_ticket_from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
