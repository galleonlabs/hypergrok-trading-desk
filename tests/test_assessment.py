from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import unittest

from trading_harness.analysis import TechnicalBias, TechnicalSnapshot
from trading_harness.assessment import (
    AssessmentVerdict,
    ProfitabilityGate,
    ProfitabilityStatus,
    build_opportunity_assessment,
)
from trading_harness.errors import ValidationError
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
)


NOW = datetime(2026, 8, 24, 16, 5, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def technical(bias: TechnicalBias = TechnicalBias.BUY, *, close_time: datetime | None = None):
    return TechnicalSnapshot(
        symbol="ETH",
        interval="4h",
        as_of=NOW,
        candle_close_time=close_time or NOW - timedelta(minutes=5),
        config_version="strategy-v1",
        config_hash=digest("config"),
        data_hash=digest("data"),
        completed_candles=1000,
        ignored_incomplete_candles=1,
        close=Decimal("2500"),
        ema_fast=Decimal("2490"),
        ema_slow=Decimal("2475"),
        ema_trend=Decimal("2400"),
        rsi=Decimal("60"),
        atr=Decimal("50"),
        bias=bias,
        stop_price=(
            None
            if bias is TechnicalBias.NOTHING
            else Decimal("2400") if bias is TechnicalBias.BUY else Decimal("2600")
        ),
        target_price=(
            None
            if bias is TechnicalBias.NOTHING
            else Decimal("2700") if bias is TechnicalBias.BUY else Decimal("2300")
        ),
        reasons=("test",),
    )


def evidence(index: int, polarity: str) -> SentimentEvidence:
    return SentimentEvidence(
        evidence_id=f"e-{index}",
        post_id=f"p-{index}",
        source_url=f"https://x.com/example/status/{index}",
        author_hash=digest(f"a-{index}"),
        content_hash=digest(f"c-{index}"),
        cluster_hash=digest(f"k-{index}"),
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW - timedelta(minutes=1),
        polarity=Decimal(polarity),
    )


def sentiment(
    polarities: list[str],
    *,
    method: CollectionMethod = CollectionMethod.X_API,
    asset_id: str = "ETH-PERP",
    complete: bool = True,
):
    return build_sentiment_snapshot(
        asset_id=asset_id,
        query="$ETH",
        query_version="q1",
        classifier_version="classifier-v1",
        method=method,
        window_start=NOW - timedelta(hours=4),
        window_end=NOW - timedelta(minutes=2),
        collected_at=NOW,
        evidence=[evidence(i, value) for i, value in enumerate(polarities)],
        excluded_count=0,
        collection_complete=complete,
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


def gate(**changes: object) -> ProfitabilityGate:
    values: dict[str, object] = {
        "gate_id": "gate-1",
        "asset_id": "ETH-PERP",
        "thesis_id": "trend-breakout",
        "thesis_version": "1",
        "strategy_version": "strategy-v1",
        "artifact_hash": digest("validation"),
        "status": ProfitabilityStatus.QUALIFIED,
        "issued_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=30),
        "oos_trades": 120,
        "shadow_closed_signals": 55,
        "net_expectancy_r": Decimal("0.15"),
        "lower_confidence_bound_r": Decimal("0.02"),
    }
    values.update(changes)
    return ProfitabilityGate(**values)  # type: ignore[arg-type]


class OpportunityAssessmentTests(unittest.TestCase):
    def test_buy_classification_can_reach_risk_quote_but_not_trade_authority(self) -> None:
        result = build_opportunity_assessment(
            assessment_id="assessment-1",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(["0", "0.1", "0", "0.1"]),
            profitability=gate(),
            at=NOW,
        )

        self.assertIs(result.verdict, AssessmentVerdict.BUY)
        self.assertTrue(result.eligible_for_risk_quote)
        self.assertFalse(result.eligible_to_trade)
        self.assertFalse(result.as_dict()["approval_created"])
        self.assertFalse(result.as_dict()["order_submitted"])

    def test_opposing_sentiment_vetoes_direction(self) -> None:
        result = build_opportunity_assessment(
            assessment_id="assessment-2",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(["-0.2", "-0.3", "-0.4", "-0.5"]),
            profitability=gate(),
            at=NOW,
        )

        self.assertIs(result.verdict, AssessmentVerdict.NOTHING)
        self.assertIn("bearish_sentiment_veto", result.reason_codes)

    def test_unknown_or_stale_evidence_is_unavailable_not_nothing(self) -> None:
        unknown = build_opportunity_assessment(
            assessment_id="assessment-3",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(["0.2"], complete=False),
            profitability=gate(),
            at=NOW,
        )
        stale = build_opportunity_assessment(
            assessment_id="assessment-4",
            asset_id="ETH-PERP",
            technical=technical(close_time=NOW - timedelta(hours=1)),
            sentiment=sentiment(["0", "0", "0", "0"]),
            profitability=gate(),
            at=NOW,
        )

        self.assertIs(unknown.verdict, AssessmentVerdict.UNAVAILABLE)
        self.assertIs(stale.verdict, AssessmentVerdict.UNAVAILABLE)

    def test_manual_browser_or_missing_profitability_remains_advisory(self) -> None:
        manual = build_opportunity_assessment(
            assessment_id="assessment-5",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(
                ["0.2", "0.3", "0.4", "0.5"],
                method=CollectionMethod.MANUAL_BROWSER,
            ),
            profitability=gate(),
            at=NOW,
        )
        unqualified = build_opportunity_assessment(
            assessment_id="assessment-6",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(["0.2", "0.3", "0.4", "0.5"]),
            profitability=None,
            at=NOW,
        )

        self.assertIs(manual.verdict, AssessmentVerdict.BUY)
        self.assertFalse(manual.eligible_for_risk_quote)
        self.assertIn("manual_sentiment_not_unattended", manual.reason_codes)
        self.assertIs(unqualified.verdict, AssessmentVerdict.BUY)
        self.assertFalse(unqualified.eligible_for_risk_quote)
        self.assertIn("profitability_gate_missing", unqualified.reason_codes)

    def test_gate_lower_bound_must_be_positive_and_asset_must_match(self) -> None:
        failed = build_opportunity_assessment(
            assessment_id="assessment-7",
            asset_id="ETH-PERP",
            technical=technical(),
            sentiment=sentiment(["0.2", "0.3", "0.4", "0.5"]),
            profitability=gate(lower_confidence_bound_r=Decimal("0")),
            at=NOW,
        )
        self.assertFalse(failed.eligible_for_risk_quote)
        self.assertIn("profitability_not_qualified", failed.reason_codes)

        with self.assertRaisesRegex(ValidationError, "different asset"):
            build_opportunity_assessment(
                assessment_id="assessment-8",
                asset_id="BTC-PERP",
                technical=technical(),
                sentiment=sentiment(["0", "0", "0", "0"]),
                profitability=None,
                at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
