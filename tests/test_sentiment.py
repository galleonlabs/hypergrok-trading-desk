from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import unittest

from trading_harness.errors import ValidationError
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentLabel,
    SentimentPolicy,
    build_sentiment_snapshot,
    sentiment_snapshot_from_dict,
)


NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def item(index: int, polarity: str, *, cluster: str | None = None) -> SentimentEvidence:
    return SentimentEvidence(
        evidence_id=f"evidence-{index}",
        post_id=f"post-{index}",
        source_url=f"https://x.com/example/status/{index}",
        author_hash=digest(f"author-{index}"),
        content_hash=digest(f"content-{index}"),
        cluster_hash=digest(cluster or f"cluster-{index}"),
        published_at=NOW - timedelta(hours=2) + timedelta(minutes=index),
        observed_at=NOW - timedelta(minutes=5),
        polarity=Decimal(polarity),
    )


def policy(**changes: object) -> SentimentPolicy:
    values: dict[str, object] = {
        "version": "test-policy-v1",
        "minimum_posts": 4,
        "minimum_authors": 4,
        "trim_fraction": Decimal("0"),
        "bullish_threshold": Decimal("0.15"),
        "bearish_threshold": Decimal("-0.15"),
        "max_cluster_share": Decimal("0.5"),
        "ttl_seconds": 900,
    }
    values.update(changes)
    return SentimentPolicy(**values)  # type: ignore[arg-type]


def snapshot(
    evidence: list[SentimentEvidence],
    *,
    method: CollectionMethod = CollectionMethod.X_API,
    complete: bool = True,
    selected_policy: SentimentPolicy | None = None,
):
    return build_sentiment_snapshot(
        asset_id="ETH-PERP",
        query="($ETH OR Ethereum) lang:en -is:retweet -is:reply",
        query_version="eth-query-v1",
        classifier_version="frozen-classifier-v1",
        method=method,
        window_start=NOW - timedelta(hours=4),
        window_end=NOW - timedelta(minutes=10),
        collected_at=NOW,
        evidence=evidence,
        excluded_count=2,
        collection_complete=complete,
        policy=selected_policy or policy(),
    )


class SentimentEvidenceTests(unittest.TestCase):
    def test_rejects_rawly_invalid_source_hash_or_time(self) -> None:
        with self.assertRaisesRegex(ValidationError, "HTTPS"):
            SentimentEvidence(
                evidence_id="e",
                post_id="p",
                source_url="http://x.com/example/status/1",
                author_hash=digest("a"),
                content_hash=digest("c"),
                cluster_hash=digest("d"),
                published_at=NOW,
                observed_at=NOW,
                polarity=Decimal("0"),
            )
        with self.assertRaisesRegex(ValidationError, "author_hash"):
            SentimentEvidence(
                evidence_id="e",
                post_id="p",
                source_url="https://x.com/example/status/1",
                author_hash="not-a-hash",
                content_hash=digest("c"),
                cluster_hash=digest("d"),
                published_at=NOW,
                observed_at=NOW,
                polarity=Decimal("0"),
            )


class SentimentSnapshotTests(unittest.TestCase):
    def test_bullish_api_snapshot_is_available_and_unattended_eligible(self) -> None:
        result = snapshot(
            [item(1, "0.2"), item(2, "0.3"), item(3, "0.4"), item(4, "0.5")]
        )

        self.assertIs(result.label, SentimentLabel.BULLISH)
        self.assertEqual(result.score, Decimal("0.35"))
        self.assertTrue(result.available)
        self.assertTrue(result.eligible_for_unattended_use)
        self.assertTrue(result.is_fresh(NOW + timedelta(minutes=5)))
        self.assertFalse(result.is_fresh(NOW + timedelta(minutes=15)))
        self.assertFalse(result.as_dict()["raw_post_text_stored"])

    def test_manual_browser_snapshot_can_inform_but_never_run_unattended(self) -> None:
        result = snapshot(
            [item(1, "0.2"), item(2, "0.3"), item(3, "0.4"), item(4, "0.5")],
            method=CollectionMethod.MANUAL_BROWSER,
        )

        self.assertTrue(result.available)
        self.assertFalse(result.eligible_for_unattended_use)

    def test_neutral_and_bearish_thresholds_are_deterministic(self) -> None:
        neutral = snapshot(
            [item(1, "-0.1"), item(2, "0"), item(3, "0.1"), item(4, "0.2")]
        )
        bearish = snapshot(
            [item(1, "-0.2"), item(2, "-0.3"), item(3, "-0.4"), item(4, "-0.5")]
        )

        self.assertIs(neutral.label, SentimentLabel.NEUTRAL)
        self.assertIs(bearish.label, SentimentLabel.BEARISH)

    def test_incomplete_insufficient_or_concentrated_collection_is_unknown(self) -> None:
        incomplete = snapshot(
            [item(1, "0.5"), item(2, "0.5"), item(3, "0.5"), item(4, "0.5")],
            complete=False,
        )
        insufficient = snapshot(
            [item(1, "0.5")],
        )
        concentrated = snapshot(
            [
                item(1, "0.5", cluster="same"),
                item(2, "0.5", cluster="same"),
                item(3, "0.5", cluster="same"),
                item(4, "0.5", cluster="other"),
            ],
            selected_policy=policy(max_cluster_share=Decimal("0.5")),
        )

        self.assertIn("collection_incomplete", incomplete.quality_reasons)
        self.assertIn("insufficient_posts", insufficient.quality_reasons)
        self.assertIn("duplicate_cluster_concentration", concentrated.quality_reasons)
        for result in (incomplete, insufficient, concentrated):
            self.assertIs(result.label, SentimentLabel.UNKNOWN)
            self.assertIsNone(result.score)
            self.assertFalse(result.available)

    def test_duplicate_author_post_or_content_is_rejected(self) -> None:
        duplicate_author = item(2, "0.2")
        duplicate_author = SentimentEvidence(
            evidence_id=duplicate_author.evidence_id,
            post_id=duplicate_author.post_id,
            source_url=duplicate_author.source_url,
            author_hash=item(1, "0.1").author_hash,
            content_hash=duplicate_author.content_hash,
            cluster_hash=duplicate_author.cluster_hash,
            published_at=duplicate_author.published_at,
            observed_at=duplicate_author.observed_at,
            polarity=duplicate_author.polarity,
        )
        with self.assertRaisesRegex(ValidationError, "author_hash"):
            snapshot([item(1, "0.1"), duplicate_author, item(3, "0.2"), item(4, "0.2")])

    def test_out_of_window_or_future_observation_is_rejected(self) -> None:
        outside = SentimentEvidence(
            evidence_id="outside",
            post_id="outside",
            source_url="https://x.com/example/status/outside",
            author_hash=digest("outside-author"),
            content_hash=digest("outside-content"),
            cluster_hash=digest("outside-cluster"),
            published_at=NOW - timedelta(hours=5),
            observed_at=NOW - timedelta(minutes=5),
            polarity=Decimal("0"),
        )
        with self.assertRaisesRegex(ValidationError, "outside"):
            snapshot([outside, item(2, "0"), item(3, "0"), item(4, "0")])

    def test_input_order_and_ambient_precision_cannot_change_artifact(self) -> None:
        items = [item(1, "0.123456789"), item(2, "0.2"), item(3, "0.3"), item(4, "0.4")]
        with localcontext() as context:
            context.prec = 6
            first = snapshot(items)
        with localcontext() as context:
            context.prec = 50
            second = snapshot(list(reversed(items)))

        self.assertEqual(first.artifact_hash, second.artifact_hash)
        self.assertEqual(first.score, second.score)

    def test_persisted_document_round_trips_and_expiry_or_policy_tampering_fails(self) -> None:
        original = snapshot(
            [item(1, "0.2"), item(2, "0.3"), item(3, "0.4"), item(4, "0.5")]
        )
        document = original.as_dict()
        self.assertEqual(sentiment_snapshot_from_dict(document), original)
        self.assertEqual(document["policy_version"], "test-policy-v1")
        self.assertRegex(document["policy_hash"], r"^[0-9a-f]{64}$")

        for field, value in (
            ("expires_at", "2099-01-01T00:00:00.000Z"),
            ("policy_hash", "f" * 64),
            ("available", False),
        ):
            tampered = deepcopy(document)
            tampered[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    sentiment_snapshot_from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
