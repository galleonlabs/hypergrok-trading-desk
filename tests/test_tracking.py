from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from trading_harness.domain import Environment
from trading_harness.errors import ValidationError
from trading_harness.tracking import (
    MarketDataNetwork,
    TrackedAsset,
    TrackingStatus,
)


NOW = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def asset(**changes: object) -> TrackedAsset:
    values: dict[str, object] = {
        "asset_id": "ETH-PERP",
        "venue": "hyperliquid",
        "market_data_network": MarketDataNetwork.MAINNET,
        "execution_environment": Environment.TESTNET,
        "symbol": "ETH",
        "interval": "4h",
        "poll_seconds": 60,
        "technical_profile_version": "trend-rsi-atr-v1",
        "sentiment_policy_version": "sentiment-quality-v1",
        "sentiment_query": "($ETH OR Ethereum) lang:en -is:retweet -is:reply",
        "sentiment_query_version": "eth-query-v1",
        "status": TrackingStatus.ACTIVE,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return TrackedAsset(**values)  # type: ignore[arg-type]


class TrackedAssetTests(unittest.TestCase):
    def test_market_data_network_is_independent_from_execution_environment(self) -> None:
        tracked = asset()

        self.assertIs(tracked.market_data_network, MarketDataNetwork.MAINNET)
        self.assertIs(tracked.execution_environment, Environment.TESTNET)
        self.assertEqual(len(tracked.config_hash), 64)

    def test_revision_changes_hash_and_preserves_identity(self) -> None:
        original = asset()
        revised = original.revise(
            updated_at=NOW + timedelta(minutes=1),
            status=TrackingStatus.PAUSED,
        )

        self.assertEqual(revised.asset_id, original.asset_id)
        self.assertEqual(revised.revision, 2)
        self.assertIs(revised.status, TrackingStatus.PAUSED)
        self.assertNotEqual(revised.config_hash, original.config_hash)

    def test_invalid_poll_revision_or_time_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "poll_seconds"):
            asset(poll_seconds=1)
        with self.assertRaisesRegex(ValidationError, "revision"):
            asset(revision=0)
        with self.assertRaisesRegex(ValidationError, "predate"):
            asset(updated_at=NOW - timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
