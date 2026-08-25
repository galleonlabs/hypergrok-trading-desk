from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.errors import StateConflict, ValidationError
from trading_harness.research_api import ResearchService
from trading_harness.research_store import ResearchStore
from trading_harness.strategy import SignalDirection
from tests.test_node import AT, history_reader
from tests.test_registered_decision import SIGNAL


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(value) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def evidence(count: int = 30) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": f"e-{index}",
            "post_id": f"p-{index}",
            "source_url": f"https://x.com/example/status/{index}",
            "author_hash": digest(f"author-{index}"),
            "content_hash": digest(f"content-{index}"),
            "cluster_hash": digest(f"cluster-{index}"),
            "published_at": iso(AT - timedelta(hours=3) + timedelta(minutes=index)),
            "observed_at": iso(AT - timedelta(seconds=1)),
            "polarity": "0",
        }
        for index in range(count)
    ]


class ResearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.temporary.name) / "research.sqlite3")
        self.service = ResearchService(
            self.store,
            clock=lambda: AT,
            history_reader=history_reader,
            analysis_bars=1001,
            validation_bars=1001,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def track(self):
        return self.service.track_asset(
            asset_id="eth",
            symbol="ETH",
            network="testnet",
            sentiment_query="$ETH OR Ethereum",
            poll_seconds=60,
        )

    def test_track_list_idempotence_and_pause_are_local_only(self) -> None:
        first = self.track()
        repeated = self.track()
        listed = self.service.list_assets()

        self.assertTrue(first["local_state_updated"])
        self.assertFalse(repeated["local_state_updated"])
        self.assertFalse(first["trade_authority_created"])
        self.assertFalse(first["order_submitted"])
        self.assertEqual(listed["count"], 1)
        self.assertFalse(listed["venue_writes_enabled"])
        paused = self.service.pause_asset(asset_id="eth", expected_revision=1)
        self.assertEqual(paused["tracked_asset"]["status"], "paused")
        self.assertFalse(paused["order_submitted"])

    def test_track_rejects_configuration_collision_and_bad_symbol(self) -> None:
        self.track()
        with self.assertRaisesRegex(StateConflict, "different configuration"):
            self.service.track_asset(
                asset_id="eth",
                symbol="BTC",
                network="testnet",
                sentiment_query="$BTC",
            )
        with self.assertRaisesRegex(ValidationError, "canonical"):
            self.service.track_asset(
                asset_id="bad",
                symbol="ETH/USDC",
                network="testnet",
                sentiment_query="$ETH",
            )

    def test_manual_browser_evidence_is_persisted_without_raw_text_or_authority(self) -> None:
        self.track()
        recorded = self.service.record_manual_sentiment(
            asset_id="eth",
            window_start=iso(AT - timedelta(hours=4)),
            window_end=iso(AT),
            evidence=evidence(),
            excluded_count=0,
            collection_complete=True,
        )
        latest = self.service.latest_sentiment("eth")

        self.assertTrue(recorded["snapshot"]["available"])
        self.assertFalse(recorded["unattended_eligible"])
        self.assertFalse(recorded["snapshot"]["raw_post_text_stored"])
        self.assertNotIn("text", repr(recorded["snapshot"]["evidence"]))
        self.assertTrue(latest["found"])
        self.assertEqual(
            latest["snapshot"]["artifact_hash"],
            recorded["snapshot"]["artifact_hash"],
        )

    def test_manual_sentiment_rejects_non_x_urls_and_non_rubric_scores(self) -> None:
        self.track()
        bad_url = evidence()
        bad_url[0]["source_url"] = "https://attacker.invalid/status/1"
        bad_score = evidence()
        bad_score[0]["polarity"] = "0.1"
        for records, message in ((bad_url, "X post URL"), (bad_score, "manual rubric")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    self.service.record_manual_sentiment(
                        asset_id="eth",
                        window_start=iso(AT - timedelta(hours=4)),
                        window_end=iso(AT),
                        evidence=records,
                        excluded_count=0,
                        collection_complete=True,
                    )

    def test_analysis_reports_registered_buy_but_remains_unqualified(self) -> None:
        self.track()
        self.service.record_manual_sentiment(
            asset_id="eth",
            window_start=iso(AT - timedelta(hours=4)),
            window_end=iso(AT),
            evidence=evidence(),
            excluded_count=0,
            collection_complete=True,
        )
        result = self.service.analyze_asset("eth")

        self.assertEqual(result["registered_signal"]["direction"], "buy")
        self.assertEqual(result["assessment"]["verdict"], "buy")
        self.assertFalse(result["assessment"]["eligible_for_risk_quote"])
        self.assertIn(
            "profitability_attestation_missing",
            result["assessment"]["reason_codes"],
        )
        self.assertIn(
            "manual_sentiment_not_unattended",
            result["assessment"]["reason_codes"],
        )
        self.assertFalse(result["profitability_attested"])
        self.assertFalse(result["venue_writes_enabled"])
        self.assertFalse(result["order_submitted"])
        self.assertRegex(result["analysis_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["analysis_record_hash"], r"^[0-9a-f]{64}$")
        persisted = self.store.get_asset_analysis(result["analysis_hash"])
        self.assertEqual(result["analysis_hash"], persisted.analysis_hash)
        self.assertEqual("eth", persisted.asset_id)
        self.assertEqual(
            result["registered_signal"]["signal_hash"],
            persisted.signal_hash,
        )

    def test_missing_sentiment_is_unavailable_for_directional_signal_while_ta_remains_visible(self) -> None:
        self.track()
        result = self.service.analyze_asset("eth")

        self.assertEqual(result["registered_signal"]["direction"], "buy")
        self.assertEqual(result["assessment"]["verdict"], "unavailable")
        self.assertEqual(
            result["assessment"]["reason_codes"],
            ["sentiment_snapshot_missing"],
        )

    def test_registered_nothing_does_not_require_sentiment_to_abstain(self) -> None:
        self.track()
        nothing = replace(
            SIGNAL,
            direction=SignalDirection.NOTHING,
            reason="no_donchian_transition",
        )
        with patch("trading_harness.research_api.latest_signal", return_value=nothing):
            result = self.service.analyze_asset("eth")

        self.assertEqual(result["assessment"]["verdict"], "nothing")
        self.assertEqual(
            result["assessment"]["reason_codes"],
            ["no_donchian_transition"],
        )
        self.assertFalse(result["assessment"]["eligible_for_risk_quote"])

    def test_historical_validation_is_not_misreported_as_deployment_or_profit_guarantee(self) -> None:
        self.track()
        result = self.service.validate_candidate("eth")

        self.assertEqual(result["historical_status"], "INCONCLUSIVE")
        self.assertTrue(result["shadow_required"])
        self.assertFalse(result["deployment_qualified"])
        self.assertFalse(result["profit_guaranteed"])
        self.assertFalse(result["order_submitted"])


if __name__ == "__main__":
    unittest.main()
