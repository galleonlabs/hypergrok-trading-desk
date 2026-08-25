from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
import hashlib
import unittest

from trading_harness.errors import ValidationError
from trading_harness.registered_decision import (
    ProfitabilityAttestation,
    ProfitabilityAttestationStatus,
    RegisteredVerdict,
    build_registered_assessment,
    registered_assessment_from_dict,
)
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
)
from trading_harness.strategy import latest_signal
from tests.test_strategy import rising_breakout_series


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


SIGNAL = latest_signal(rising_breakout_series())
assert SIGNAL is not None
AT = SIGNAL.observed_at


def sentiment(
    *,
    polarity: str = "0",
    method: CollectionMethod = CollectionMethod.X_API,
):
    evidence = SentimentEvidence(
        evidence_id="e-1",
        post_id="p-1",
        source_url="https://x.com/example/status/1",
        author_hash=digest("author"),
        content_hash=digest("content"),
        cluster_hash=digest("cluster"),
        published_at=AT - timedelta(minutes=30),
        observed_at=AT - timedelta(minutes=1),
        polarity=Decimal(polarity),
    )
    return build_sentiment_snapshot(
        asset_id="ETH-PERP",
        query="$ETH OR Ethereum",
        query_version="q1",
        classifier_version="classifier-v1",
        method=method,
        window_start=AT - timedelta(hours=4),
        window_end=AT - timedelta(seconds=30),
        collected_at=AT,
        evidence=(evidence,),
        excluded_count=0,
        collection_complete=True,
        policy=SentimentPolicy(
            minimum_posts=1,
            minimum_authors=1,
            trim_fraction=Decimal("0"),
            max_cluster_share=Decimal("1"),
        ),
    )


def attestation(**changes: object) -> ProfitabilityAttestation:
    values: dict[str, object] = {
        "attestation_id": "profit-1",
        "asset_id": "ETH-PERP",
        "strategy_hash": SIGNAL.strategy_hash,
        "validation_artifact_hash": digest("validation"),
        "shadow_artifact_hash": digest("shadow"),
        "status": ProfitabilityAttestationStatus.QUALIFIED,
        "issued_at": AT - timedelta(days=1),
        "expires_at": AT + timedelta(days=30),
        "oos_trades": 120,
        "oos_expectancy_r": Decimal("0.2"),
        "oos_lower_bound_r": Decimal("0.03"),
        "shadow_closed_signals": 55,
        "shadow_elapsed_days": 90,
        "shadow_expectancy_r": Decimal("0.15"),
        "shadow_lower_bound_r": Decimal("0.01"),
        "cost_stress_positive": True,
        "sentiment_incremental_passed": True,
        "drift_passed": True,
        "independent_reviewed": True,
    }
    values.update(changes)
    return ProfitabilityAttestation(**values)  # type: ignore[arg-type]


class RegisteredAssessmentTests(unittest.TestCase):
    def test_qualified_registered_buy_has_mandatory_bracket_but_no_trade_authority(self) -> None:
        result = build_registered_assessment(
            assessment_id="decision-1",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(),
            profitability=attestation(),
            at=AT,
        )

        self.assertIs(result.verdict, RegisteredVerdict.BUY)
        self.assertTrue(result.eligible_for_risk_quote)
        self.assertFalse(result.eligible_to_trade)
        self.assertLess(result.stop_price, result.reference_price)
        self.assertGreater(result.target_price, result.reference_price)
        with localcontext() as context:
            context.prec = 96
            risk = result.reference_price - result.stop_price
            reward = result.target_price - result.reference_price
            self.assertEqual(reward, risk * Decimal("2"))
        self.assertFalse(result.as_dict()["order_submitted"])
        self.assertEqual(result, registered_assessment_from_dict(result.as_dict()))
        tampered = result.as_dict()
        tampered["instrument"] = "BTC"
        with self.assertRaisesRegex(ValidationError, "artifact_hash"):
            registered_assessment_from_dict(tampered)

    def test_missing_gate_or_manual_browser_is_advisory_only(self) -> None:
        missing = build_registered_assessment(
            assessment_id="missing",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(),
            profitability=None,
            at=AT,
        )
        manual = build_registered_assessment(
            assessment_id="manual",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(method=CollectionMethod.MANUAL_BROWSER),
            profitability=attestation(),
            at=AT,
        )

        self.assertIs(missing.verdict, RegisteredVerdict.BUY)
        self.assertFalse(missing.eligible_for_risk_quote)
        self.assertIn("profitability_attestation_missing", missing.reason_codes)
        self.assertIs(manual.verdict, RegisteredVerdict.BUY)
        self.assertFalse(manual.eligible_for_risk_quote)
        self.assertIn("manual_sentiment_not_unattended", manual.reason_codes)

        attended = build_registered_assessment(
            assessment_id="manual-attended",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(method=CollectionMethod.MANUAL_BROWSER),
            profitability=attestation(),
            at=AT,
            attended=True,
        )
        self.assertIs(attended.verdict, RegisteredVerdict.BUY)
        self.assertTrue(attended.eligible_for_risk_quote)
        self.assertFalse(attended.eligible_to_trade)
        self.assertIn(
            "manual_sentiment_requires_attended_approval",
            attended.reason_codes,
        )

    def test_opposing_sentiment_vetoes_and_removes_bracket(self) -> None:
        result = build_registered_assessment(
            assessment_id="veto",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(polarity="-0.8"),
            profitability=attestation(),
            at=AT,
        )

        self.assertIs(result.verdict, RegisteredVerdict.NOTHING)
        self.assertIsNone(result.reference_price)
        self.assertIsNone(result.stop_price)
        self.assertIsNone(result.target_price)
        self.assertFalse(result.eligible_for_risk_quote)

    def test_profitability_gate_requires_every_historical_and_shadow_threshold(self) -> None:
        cases = (
            {"oos_trades": 99},
            {"oos_lower_bound_r": Decimal("0")},
            {"shadow_closed_signals": 49},
            {"shadow_elapsed_days": 89},
            {"shadow_lower_bound_r": Decimal("0")},
            {"cost_stress_positive": False},
            {"sentiment_incremental_passed": False},
            {"drift_passed": False},
            {"independent_reviewed": False},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                gate = attestation(**changes)
                self.assertFalse(gate.is_active(AT))
                result = build_registered_assessment(
                    assessment_id="threshold-" + next(iter(changes)),
                    asset_id="ETH-PERP",
                    signal=SIGNAL,
                    sentiment=sentiment(),
                    profitability=gate,
                    at=AT,
                )
                self.assertFalse(result.eligible_for_risk_quote)
                self.assertIn("profitability_not_qualified", result.reason_codes)

    def test_stale_signal_and_cross_asset_evidence_fail_closed(self) -> None:
        stale = build_registered_assessment(
            assessment_id="stale",
            asset_id="ETH-PERP",
            signal=SIGNAL,
            sentiment=sentiment(),
            profitability=attestation(),
            at=SIGNAL.expires_at,
        )
        self.assertIs(stale.verdict, RegisteredVerdict.UNAVAILABLE)
        self.assertFalse(stale.is_fresh(SIGNAL.expires_at))

        with self.assertRaisesRegex(ValidationError, "different asset"):
            build_registered_assessment(
                assessment_id="wrong",
                asset_id="BTC-PERP",
                signal=SIGNAL,
                sentiment=sentiment(),
                profitability=None,
                at=AT,
            )

    def test_artifact_hash_detects_tampering_and_math_ignores_ambient_context(self) -> None:
        arguments = {
            "assessment_id": "stable",
            "asset_id": "ETH-PERP",
            "signal": SIGNAL,
            "sentiment": sentiment(),
            "profitability": attestation(),
            "at": AT,
        }
        with localcontext() as context:
            context.prec = 6
            first = build_registered_assessment(**arguments)
        with localcontext() as context:
            context.prec = 50
            second = build_registered_assessment(**arguments)
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValidationError, "artifact_hash"):
            replace(first, target_price=first.target_price + Decimal("1"))


if __name__ == "__main__":
    unittest.main()
