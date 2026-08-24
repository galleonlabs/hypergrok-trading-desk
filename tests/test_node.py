from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from trading_harness.canonical import canonical_decimal
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict
from trading_harness.history import fetch_candle_history
from trading_harness.node import ResearchNode, node_status
from trading_harness.research_store import ResearchStore
from trading_harness.tracking import (
    MarketDataNetwork,
    TrackedAsset,
    TrackingStatus,
)
from tests.test_strategy import START, rising_breakout_series


SERIES = rising_breakout_series()
AT = SERIES[-1].close_time + timedelta(minutes=1)


def tracked() -> TrackedAsset:
    return TrackedAsset(
        asset_id="eth-4h",
        venue="hyperliquid",
        market_data_network=MarketDataNetwork.TESTNET,
        execution_environment=Environment.SHADOW,
        symbol="ETH",
        interval="4h",
        poll_seconds=10,
        technical_profile_version="trend-rsi-atr-v1",
        sentiment_policy_version="sentiment-quality-v1",
        sentiment_query="$ETH OR Ethereum",
        sentiment_query_version="q1",
        status=TrackingStatus.ACTIVE,
        revision=1,
        created_at=START,
        updated_at=START,
    )


RAW = [
    {
        "t": int(candle.open_time.timestamp() * 1000),
        "T": int(candle.close_time.timestamp() * 1000) - 1,
        "s": candle.instrument,
        "i": candle.interval,
        "o": canonical_decimal(candle.open),
        "h": canonical_decimal(candle.high),
        "l": canonical_decimal(candle.low),
        "c": canonical_decimal(candle.close),
        "v": canonical_decimal(candle.volume),
        "n": 1,
    }
    for candle in SERIES
]


def history_reader(asset: TrackedAsset, start: int, end: int, at: datetime):
    return fetch_candle_history(
        asset.symbol,
        asset.interval,
        start,
        end,
        asset.market_data_network.value,
        transport=lambda _endpoint, _payload: RAW,
        clock=lambda: at,
    )


class ResearchNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "research.sqlite3"
        self.store = ResearchStore(self.path)
        self.store.upsert_tracked_asset(tracked())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def node(self, instance: str = "instance-1", **changes: object) -> ResearchNode:
        arguments: dict[str, object] = {
            "store": self.store,
            "node_id": "desk-node",
            "instance_id": instance,
            "clock": lambda: AT,
            "history_reader": history_reader,
            "history_bars": 1001,
            "lease_ttl_seconds": 30,
            "heartbeat_ttl_seconds": 20,
        }
        arguments.update(changes)
        return ResearchNode(**arguments)  # type: ignore[arg-type]

    def test_cycle_persists_research_and_stable_registered_signal(self) -> None:
        node = self.node()
        runtime = node.start(at=AT)
        first = node.run_cycle(at=AT, force=True)
        second = node.run_cycle(at=AT + timedelta(seconds=1), force=True)

        self.assertEqual(runtime.process_state, "running")
        self.assertEqual(runtime.risk_gate, "halted")
        self.assertEqual(first.process_state, "running")
        self.assertEqual(first.risk_gate, "halted")
        self.assertEqual(first.assets[0].signal_direction, "buy")
        self.assertEqual(first.assets[0].candles_persisted, 1001)
        self.assertEqual(second.assets[0].candles_persisted, 0)
        self.assertEqual(first.assets[0].history_hash, second.assets[0].history_hash)
        self.assertEqual(first.assets[0].technical_hash, second.assets[0].technical_hash)
        self.assertEqual(first.assets[0].signal_hash, second.assets[0].signal_hash)
        self.assertEqual(
            len(self.store.list_artifacts("eth-4h", "candle", limit=2_000)),
            1001,
        )
        self.assertEqual(
            len(self.store.list_artifacts("eth-4h", "technical")),
            1,
        )

        status = node_status(self.store, "desk-node", at=AT + timedelta(seconds=1))
        self.assertTrue(status["available"])
        self.assertEqual(status["capability"], "research_only")
        self.assertFalse(status["venue_writes_enabled"])
        self.assertFalse(status["credential_loading_enabled"])
        self.assertEqual(
            {heartbeat["component"] for heartbeat in status["heartbeats"]},
            {"asset:eth-4h", "scheduler"},
        )

    def test_poll_schedule_skips_not_yet_due_asset(self) -> None:
        node = self.node()
        node.start(at=AT)
        node.run_cycle(at=AT, force=True)
        skipped = node.run_cycle(at=AT + timedelta(seconds=5))

        self.assertEqual(skipped.assets, ())
        self.assertEqual(skipped.failures, ())

    def test_bad_asset_cycle_degrades_and_sanitizes_failure(self) -> None:
        def broken(
            asset: TrackedAsset,
            start: int,
            end: int,
            at: datetime,
        ):
            del asset, start, end, at
            raise RuntimeError("PRIVATE RESPONSE BODY")

        node = self.node(history_reader=broken)
        node.start(at=AT)
        result = node.run_cycle(at=AT, force=True)

        self.assertEqual(result.process_state, "degraded")
        self.assertEqual(result.risk_gate, "halted")
        self.assertEqual(
            result.failures,
            ({"asset_id": "eth-4h", "error_type": "RuntimeError"},),
        )
        self.assertNotIn("PRIVATE RESPONSE", repr(result.as_dict()))
        heartbeat = self.store.list_heartbeats("desk-node")[0]
        self.assertEqual(heartbeat.status, "failed")
        self.assertNotIn("PRIVATE RESPONSE", repr(heartbeat.details))

    def test_singleton_fencing_stop_and_restart(self) -> None:
        first = self.node("instance-1")
        first.start(at=AT)
        with self.assertRaisesRegex(StateConflict, "another instance"):
            self.node("instance-2").start(at=AT + timedelta(seconds=1))

        stopped = first.stop(at=AT + timedelta(seconds=2))
        self.assertEqual(stopped.process_state, "stopped")
        self.assertEqual(stopped.risk_gate, "halted")
        restarted = self.node("instance-2")
        running = restarted.start(at=AT + timedelta(seconds=2))
        self.assertEqual(running.generation, stopped.generation + 1)
        self.assertEqual(running.process_state, "running")

    def test_missing_node_status_is_explicitly_halted(self) -> None:
        status = node_status(self.store, "missing", at=AT)
        self.assertFalse(status["available"])
        self.assertEqual(status["process_state"], "stopped")
        self.assertEqual(status["risk_gate"], "halted")


if __name__ == "__main__":
    unittest.main()
