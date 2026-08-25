from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from trading_harness.analysis import Candle, TechnicalBias, TechnicalSnapshot
from trading_harness.assessment import build_opportunity_assessment
from trading_harness.canonical import canonical_json
from trading_harness.domain import Environment
from trading_harness.errors import RecordNotFound, StateConflict, StorageError, ValidationError
from trading_harness.history import HistoricalCandle
from trading_harness.planning import RiskTicket, RiskTicketStatus
from trading_harness.research_store import RESEARCH_SCHEMA_VERSION, ResearchStore
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
)
from trading_harness.store import SQLiteStore
from trading_harness.tracking import MarketDataNetwork, TrackedAsset, TrackingStatus


NOW = datetime(2026, 8, 24, 16, 0, 0, 123456, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def tracked_asset(
    asset_id: str = "eth-1h",
    *,
    symbol: str = "ETH",
    interval: str = "1h",
    revision: int = 1,
    status: TrackingStatus = TrackingStatus.ACTIVE,
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
    poll_seconds: int = 60,
) -> TrackedAsset:
    return TrackedAsset(
        asset_id=asset_id,
        venue="hyperliquid",
        market_data_network=MarketDataNetwork.MAINNET,
        execution_environment=Environment.SHADOW,
        symbol=symbol,
        interval=interval,
        poll_seconds=poll_seconds,
        technical_profile_version="trend-rsi-atr-v1",
        sentiment_policy_version="sentiment-quality-v1",
        sentiment_query=f"{symbol} OR ${symbol}",
        sentiment_query_version="query-v1",
        status=status,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
    )


def candle(
    sequence: int = 0,
    *,
    symbol: str = "ETH",
    interval: str = "1h",
    high: str = "101",
) -> Candle:
    opened = NOW - timedelta(hours=3 - sequence)
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("42"),
    )


def technical() -> TechnicalSnapshot:
    return TechnicalSnapshot(
        symbol="ETH",
        interval="1h",
        as_of=NOW,
        candle_close_time=NOW - timedelta(seconds=1),
        config_version="trend-rsi-atr-v1",
        config_hash=HASH_A,
        data_hash=HASH_B,
        completed_candles=200,
        ignored_incomplete_candles=1,
        close=Decimal("3000"),
        ema_fast=Decimal("2995"),
        ema_slow=Decimal("2980"),
        ema_trend=Decimal("2900"),
        rsi=Decimal("60"),
        atr=Decimal("25"),
        bias=TechnicalBias.BUY,
        stop_price=Decimal("2950"),
        target_price=Decimal("3150"),
        reasons=("close_above_trend", "fast_above_slow", "rsi_buy_band"),
        executable=False,
    )


def sentiment():
    evidence = SentimentEvidence(
        evidence_id="ev-1",
        post_id="post-1",
        source_url="https://x.com/example/status/1",
        author_hash=HASH_A,
        content_hash=HASH_B,
        cluster_hash=HASH_C,
        published_at=NOW - timedelta(minutes=5),
        observed_at=NOW - timedelta(minutes=1),
        polarity=Decimal("0.5"),
    )
    return build_sentiment_snapshot(
        asset_id="eth-1h",
        query="ETH OR $ETH",
        query_version="query-v1",
        classifier_version="classifier-v1",
        method=CollectionMethod.X_API,
        window_start=NOW - timedelta(hours=1),
        window_end=NOW - timedelta(seconds=30),
        collected_at=NOW,
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


class ResearchStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "research.sqlite"
        self.store = ResearchStore(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class MigrationTests(ResearchStoreTestCase):
    def test_schema_is_checksummed_wal_and_can_share_the_capital_database(self) -> None:
        capital_path = Path(self.temporary.name) / "combined.sqlite"
        SQLiteStore(capital_path)
        ResearchStore(capital_path)
        connection = sqlite3.connect(capital_path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            migrations = connection.execute(
                "SELECT version, name, checksum, applied_at FROM research_schema_migrations"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual("wal", mode)
        self.assertEqual(RESEARCH_SCHEMA_VERSION, migrations[-1][0])
        self.assertEqual(64, len(migrations[-1][2]))
        self.assertTrue(migrations[-1][3].endswith("Z"))
        self.assertIn("commands", tables)
        self.assertIn("research_artifacts", tables)

    def test_restart_is_idempotent_and_tampered_migration_fails_closed(self) -> None:
        ResearchStore(self.path)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                RESEARCH_SCHEMA_VERSION,
                connection.execute(
                    "SELECT count(*) FROM research_schema_migrations"
                ).fetchone()[0],
            )
            connection.execute(
                "UPDATE research_schema_migrations SET checksum = ? WHERE version = 1",
                (HASH_A,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            ResearchStore(self.path)

    def test_recorded_migration_with_missing_table_fails_closed(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TABLE research_node_heartbeats")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            ResearchStore(self.path)


class TrackedAssetPersistenceTests(ResearchStoreTestCase):
    def test_create_idempotence_list_pause_and_microsecond_restart(self) -> None:
        original = tracked_asset()
        self.assertEqual(original, self.store.upsert_tracked_asset(original))
        self.assertEqual(original, self.store.upsert_tracked_asset(original))
        restarted = ResearchStore(self.path)
        loaded = restarted.get_tracked_asset(original.asset_id)
        self.assertEqual(NOW, loaded.created_at)
        self.assertEqual(NOW, loaded.updated_at)
        self.assertEqual((loaded,), restarted.list_tracked_assets(status="active"))

        paused_at = NOW + timedelta(microseconds=1)
        paused = restarted.pause_tracked_asset(
            original.asset_id,
            expected_revision=1,
            at=paused_at,
        )
        self.assertEqual(2, paused.revision)
        self.assertIs(TrackingStatus.PAUSED, paused.status)
        self.assertEqual(paused_at, ResearchStore(self.path).get_tracked_asset("eth-1h").updated_at)
        self.assertEqual((paused,), restarted.list_tracked_assets(status="paused"))

    def test_updates_require_exact_cas_and_cannot_change_identity(self) -> None:
        original = tracked_asset()
        self.store.upsert_tracked_asset(original)
        revised = original.revise(
            updated_at=NOW + timedelta(seconds=1),
            poll_seconds=120,
        )
        self.store.upsert_tracked_asset(revised, expected_revision=1)
        with self.assertRaises(StateConflict):
            self.store.upsert_tracked_asset(revised, expected_revision=1)
        with self.assertRaises(StateConflict):
            self.store.upsert_tracked_asset(
                replace(
                    revised,
                    symbol="BTC",
                    revision=3,
                    updated_at=NOW + timedelta(seconds=2),
                ),
                expected_revision=2,
            )
        with self.assertRaises(StateConflict):
            self.store.pause_tracked_asset(
                original.asset_id,
                expected_revision=1,
                at=NOW + timedelta(seconds=3),
            )

    def test_natural_series_identity_is_unique(self) -> None:
        self.store.upsert_tracked_asset(tracked_asset())
        with self.assertRaises(StateConflict):
            self.store.upsert_tracked_asset(tracked_asset(asset_id="duplicate"))

    def test_concurrent_compare_and_swap_has_one_winner(self) -> None:
        original = tracked_asset()
        self.store.upsert_tracked_asset(original)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def update(poll_seconds: int) -> None:
            store = ResearchStore(self.path)
            candidate = original.revise(
                updated_at=NOW + timedelta(seconds=poll_seconds),
                poll_seconds=poll_seconds,
            )
            barrier.wait()
            try:
                store.upsert_tracked_asset(candidate, expected_revision=1)
                outcome = "success"
            except StateConflict:
                outcome = "conflict"
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=update, args=(120,)),
            threading.Thread(target=update, args=(180,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["success", "conflict"], outcomes)
        self.assertEqual(2, self.store.get_tracked_asset("eth-1h").revision)

    def test_tampered_payload_is_detected_on_read(self) -> None:
        self.store.upsert_tracked_asset(tracked_asset())
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE research_tracked_assets SET payload_json = '{}' WHERE asset_id = 'eth-1h'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_tracked_asset("eth-1h")


class ImmutableArtifactTests(ResearchStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_tracked_asset(tracked_asset())

    def test_candles_are_canonical_idempotent_ordered_and_correction_closed(self) -> None:
        first = candle(0)
        second = candle(1)
        first_record = self.store.put_candle(
            "eth-1h", first, stored_at=first.close_time + timedelta(seconds=1)
        )
        self.assertEqual(
            first_record,
            self.store.put_candle(
                "eth-1h", first, stored_at=first.close_time + timedelta(seconds=30)
            ),
        )
        second_record = self.store.put_candle(
            "eth-1h", second, stored_at=second.close_time + timedelta(seconds=1)
        )
        listed = self.store.list_artifacts(
            "eth-1h", "candle", series_key="1h"
        )
        self.assertEqual((first_record, second_record), listed)
        self.assertEqual(
            hashlib.sha256(first_record.payload_json.encode()).hexdigest(),
            first_record.content_hash,
        )
        self.assertEqual(first.canonical_record()["open_time"], first_record.payload["open_time"])

        corrected = candle(0, high="102")
        with self.assertRaises(StateConflict):
            self.store.put_candle(
                "eth-1h",
                corrected,
                stored_at=corrected.close_time + timedelta(seconds=2),
            )

    def test_artifact_time_and_asset_series_are_enforced(self) -> None:
        target = candle()
        with self.assertRaises(ValidationError):
            self.store.put_candle(
                "eth-1h", target, stored_at=target.close_time - timedelta(seconds=1)
            )
        with self.assertRaises(StateConflict):
            self.store.put_candle(
                "eth-1h",
                candle(symbol="BTC"),
                stored_at=target.close_time + timedelta(seconds=1),
            )
        with self.assertRaises(RecordNotFound):
            self.store.put_candle(
                "missing", target, stored_at=target.close_time + timedelta(seconds=1)
            )

    def test_historical_candle_preserves_trade_count_and_venue_record(self) -> None:
        delta = (NOW - timedelta(hours=2)) - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        opened_ms = (
            (delta.days * 86_400 + delta.seconds) * 1_000
            + delta.microseconds // 1_000
        )
        opened_ms -= opened_ms % 3_600_000
        historical = HistoricalCandle(
            symbol="ETH",
            interval="1h",
            open_time_ms=opened_ms,
            close_time_ms=opened_ms + 3_600_000 - 1,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("42"),
            trade_count=17,
        )
        stored_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=historical.close_time_ms + 1
        )
        record = self.store.put_candle(
            "eth-1h", historical, stored_at=stored_at
        )
        self.assertEqual(17, record.payload["n"])
        self.assertEqual(opened_ms, record.payload["t"])
        self.assertEqual(
            "hyperliquid.historical_candle.v1",
            record.payload["schema_version"],
        )

    def test_full_research_chain_is_immutable_and_survives_restart(self) -> None:
        technical_snapshot = technical()
        sentiment_snapshot = sentiment()
        technical_record = self.store.put_technical(
            "eth-1h", technical_snapshot, stored_at=NOW + timedelta(seconds=1)
        )
        sentiment_record = self.store.put_sentiment(
            sentiment_snapshot, stored_at=NOW + timedelta(seconds=1)
        )
        assessment = build_opportunity_assessment(
            assessment_id="assessment-1",
            asset_id="eth-1h",
            technical=technical_snapshot,
            sentiment=sentiment_snapshot,
            profitability=None,
            at=NOW,
        )
        assessment_record = self.store.put_assessment(
            assessment, stored_at=NOW + timedelta(seconds=1)
        )
        ticket = RiskTicket(
            ticket_id="ticket-1",
            assessment_hash=assessment.artifact_hash,
            account_snapshot_hash=HASH_C,
            policy_version="risk-v1",
            policy_hash=HASH_A,
            created_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=61),
            status=RiskTicketStatus.DENIED,
            reason_codes=("assessment_not_risk_eligible",),
            risk_budget=Decimal("0"),
            quantity=Decimal("0"),
            expected_loss=Decimal("0"),
            stressed_loss=Decimal("0"),
            expected_reward=Decimal("0"),
            net_reward_risk=None,
            catastrophic_loss_bound=Decimal("1000"),
            plan=None,
        )
        ticket_record = self.store.put_risk_ticket(
            "eth-1h", ticket, stored_at=NOW + timedelta(seconds=2)
        )

        restarted = ResearchStore(self.path)
        self.assertEqual(
            technical_record,
            restarted.get_artifact_by_hash("technical", assessment.technical_hash),
        )
        self.assertEqual(
            sentiment_record,
            restarted.get_artifact_by_hash("sentiment", assessment.sentiment_hash),
        )
        self.assertEqual(
            assessment_record,
            restarted.get_artifact("assessment", "assessment-1"),
        )
        self.assertEqual(ticket_record, restarted.get_artifact("risk_ticket", "ticket-1"))
        self.assertFalse(assessment_record.payload["eligible_to_trade"])
        self.assertFalse(ticket_record.payload["order_submitted"])

        changed_assessment = replace(
            assessment,
            reason_codes=("changed",),
            artifact_hash=HASH_A,
        )
        with self.assertRaises(StateConflict):
            restarted.put_assessment(
                changed_assessment, stored_at=NOW + timedelta(seconds=3)
            )

    def test_assessment_and_risk_dependencies_fail_closed(self) -> None:
        technical_snapshot = technical()
        sentiment_snapshot = sentiment()
        assessment = build_opportunity_assessment(
            assessment_id="assessment-1",
            asset_id="eth-1h",
            technical=technical_snapshot,
            sentiment=sentiment_snapshot,
            profitability=None,
            at=NOW,
        )
        with self.assertRaises(StateConflict):
            self.store.put_assessment(assessment, stored_at=NOW + timedelta(seconds=1))

        ticket = RiskTicket(
            ticket_id="ticket-missing",
            assessment_hash=assessment.artifact_hash,
            account_snapshot_hash=HASH_C,
            policy_version="risk-v1",
            policy_hash=HASH_A,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            status=RiskTicketStatus.DENIED,
            reason_codes=("denied",),
            risk_budget=Decimal("0"),
            quantity=Decimal("0"),
            expected_loss=Decimal("0"),
            stressed_loss=Decimal("0"),
            expected_reward=Decimal("0"),
            net_reward_risk=None,
            catastrophic_loss_bound=Decimal("1000"),
            plan=None,
        )
        with self.assertRaises(StateConflict):
            self.store.put_risk_ticket(
                "eth-1h", ticket, stored_at=NOW + timedelta(seconds=1)
            )

    def test_tampered_artifact_payload_is_detected(self) -> None:
        target = candle()
        record = self.store.put_candle(
            "eth-1h", target, stored_at=target.close_time + timedelta(seconds=1)
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                UPDATE research_artifacts SET payload_json = ?
                WHERE artifact_kind = 'candle' AND artifact_id = ?
                """,
                (canonical_json({"tampered": True}), record.artifact_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_artifact("candle", record.artifact_id)

    def test_tampered_artifact_metadata_is_detected(self) -> None:
        target = candle()
        record = self.store.put_candle(
            "eth-1h", target, stored_at=target.close_time + timedelta(seconds=1)
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                UPDATE research_artifacts SET stored_at = ?
                WHERE artifact_kind = 'candle' AND artifact_id = ?
                """,
                (
                    (target.close_time + timedelta(seconds=2))
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                    record.artifact_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_artifact("candle", record.artifact_id)


class NodeLeaseAndRuntimeTests(ResearchStoreTestCase):
    def test_lease_is_singleton_fenced_renewable_and_restart_durable(self) -> None:
        first = self.store.acquire_node_lease(
            "desk-node", "instance-1", at=NOW, ttl_seconds=10
        )
        self.assertEqual(1, first.fencing_token)
        self.assertEqual(
            first,
            self.store.acquire_node_lease(
                "desk-node", "instance-1", at=NOW + timedelta(seconds=1), ttl_seconds=30
            ),
        )
        with self.assertRaises(StateConflict):
            self.store.acquire_node_lease(
                "desk-node", "instance-2", at=NOW + timedelta(seconds=5), ttl_seconds=10
            )
        renewed = self.store.renew_node_lease(
            "desk-node",
            "instance-1",
            1,
            at=NOW + timedelta(seconds=5),
            ttl_seconds=10,
        )
        self.assertEqual(NOW + timedelta(seconds=15), renewed.expires_at)
        with self.assertRaises(StateConflict):
            self.store.renew_node_lease(
                "desk-node",
                "instance-1",
                1,
                at=NOW + timedelta(seconds=15),
                ttl_seconds=10,
            )

        restarted = ResearchStore(self.path)
        second = restarted.acquire_node_lease(
            "desk-node", "instance-2", at=NOW + timedelta(seconds=15), ttl_seconds=20
        )
        self.assertEqual(2, second.fencing_token)
        with self.assertRaises(StateConflict):
            restarted.release_node_lease(
                "desk-node", "instance-1", 1, at=NOW + timedelta(seconds=16)
            )
        released = restarted.release_node_lease(
            "desk-node", "instance-2", 2, at=NOW + timedelta(seconds=16)
        )
        self.assertEqual("released", released.state)
        self.assertFalse(released.is_active(NOW + timedelta(seconds=16)))

    def test_runtime_is_cas_driven_halted_on_start_and_fenced(self) -> None:
        lease = self.store.acquire_node_lease(
            "desk-node", "instance-1", at=NOW, ttl_seconds=30
        )
        runtime = self.store.start_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            capability="research_only",
            at=NOW,
            details={"recovery": "pending"},
        )
        self.assertEqual("starting", runtime.process_state)
        self.assertEqual("halted", runtime.risk_gate)
        with self.assertRaises(StateConflict):
            self.store.start_node_runtime(
                "desk-node",
                "instance-1",
                lease.fencing_token,
                capability="research_only",
                at=NOW + timedelta(seconds=1),
            )

        running = self.store.update_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            expected_revision=runtime.revision,
            process_state="running",
            risk_gate="halted",
            at=NOW + timedelta(seconds=1),
            details={"recovery": "complete"},
        )
        self.assertEqual(2, running.revision)
        with self.assertRaises(StateConflict):
            self.store.update_node_runtime(
                "desk-node",
                "instance-1",
                lease.fencing_token,
                expected_revision=runtime.revision,
                process_state="running",
                risk_gate="halted",
                at=NOW + timedelta(seconds=2),
            )
        with self.assertRaises(ValidationError):
            self.store.update_node_runtime(
                "desk-node",
                "instance-1",
                lease.fencing_token,
                expected_revision=running.revision,
                process_state="degraded",
                risk_gate="ready",
                at=NOW + timedelta(seconds=2),
            )
        with self.assertRaises(ValidationError):
            self.store.update_node_runtime(
                "desk-node",
                "instance-1",
                lease.fencing_token,
                expected_revision=running.revision,
                process_state="running",
                risk_gate="ready",
                at=NOW + timedelta(seconds=2),
            )
        degraded = self.store.update_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            expected_revision=running.revision,
            process_state="degraded",
            risk_gate="halted",
            at=NOW + timedelta(seconds=2),
        )
        self.assertEqual("halted", degraded.risk_gate)
        self.assertEqual(degraded, ResearchStore(self.path).get_node_runtime("desk-node"))
        with self.assertRaises(StateConflict):
            self.store.release_node_lease(
                "desk-node",
                "instance-1",
                lease.fencing_token,
                at=NOW + timedelta(seconds=3),
            )
        stopping = self.store.update_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            expected_revision=degraded.revision,
            process_state="stopping",
            risk_gate="halted",
            at=NOW + timedelta(seconds=3),
        )
        stopped = self.store.update_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            expected_revision=stopping.revision,
            process_state="stopped",
            risk_gate="halted",
            at=NOW + timedelta(seconds=4),
        )
        self.assertEqual("stopped", stopped.process_state)
        released = self.store.release_node_lease(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            at=NOW + timedelta(seconds=5),
        )
        self.assertEqual("released", released.state)

    def test_heartbeats_are_monotonic_integrity_checked_and_fenced(self) -> None:
        lease = self.store.acquire_node_lease(
            "desk-node", "instance-1", at=NOW, ttl_seconds=30
        )
        self.store.start_node_runtime(
            "desk-node",
            "instance-1",
            lease.fencing_token,
            capability="research_only",
            at=NOW,
        )
        heartbeat = self.store.heartbeat(
            "desk-node",
            "scheduler",
            "instance-1",
            lease.fencing_token,
            status="healthy",
            at=NOW + timedelta(seconds=1),
            ttl_seconds=5,
            details={"jobs": 3},
        )
        self.assertTrue(heartbeat.is_fresh(NOW + timedelta(seconds=2)))
        self.assertEqual(
            heartbeat,
            self.store.heartbeat(
                "desk-node",
                "scheduler",
                "instance-1",
                lease.fencing_token,
                status="healthy",
                at=NOW + timedelta(seconds=1),
                ttl_seconds=5,
                details={"jobs": 3},
            ),
        )
        with self.assertRaises(StateConflict):
            self.store.heartbeat(
                "desk-node",
                "scheduler",
                "instance-1",
                lease.fencing_token,
                status="failed",
                at=NOW + timedelta(seconds=1),
                ttl_seconds=5,
            )
        with self.assertRaises(StateConflict):
            self.store.heartbeat(
                "desk-node",
                "scheduler",
                "instance-1",
                lease.fencing_token,
                status="healthy",
                at=NOW,
                ttl_seconds=5,
            )
        lease_bounded = self.store.heartbeat(
            "desk-node",
            "watchdog",
            "instance-1",
            lease.fencing_token,
            status="healthy",
            at=NOW + timedelta(seconds=2),
            ttl_seconds=60,
        )
        self.assertEqual(lease.expires_at, lease_bounded.valid_until)
        self.assertEqual(
            (heartbeat, lease_bounded),
            ResearchStore(self.path).list_heartbeats("desk-node"),
        )

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                UPDATE research_node_heartbeats SET details_json = '{"jobs":4}'
                WHERE node_id = 'desk-node' AND component = 'scheduler'
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.list_heartbeats("desk-node")

    def test_expired_owner_cannot_heartbeat_after_takeover(self) -> None:
        first = self.store.acquire_node_lease(
            "desk-node", "instance-1", at=NOW, ttl_seconds=5
        )
        self.store.start_node_runtime(
            "desk-node",
            "instance-1",
            first.fencing_token,
            capability="research_only",
            at=NOW,
        )
        second = self.store.acquire_node_lease(
            "desk-node", "instance-2", at=NOW + timedelta(seconds=5), ttl_seconds=10
        )
        second_runtime = self.store.start_node_runtime(
            "desk-node",
            "instance-2",
            second.fencing_token,
            capability="research_only",
            at=NOW + timedelta(seconds=5),
        )
        self.assertEqual(2, second_runtime.generation)
        with self.assertRaises(StateConflict):
            self.store.heartbeat(
                "desk-node",
                "scheduler",
                "instance-1",
                first.fencing_token,
                status="healthy",
                at=NOW + timedelta(seconds=6),
                ttl_seconds=5,
            )
        current = self.store.heartbeat(
            "desk-node",
            "scheduler",
            "instance-2",
            second.fencing_token,
            status="healthy",
            at=NOW + timedelta(seconds=6),
            ttl_seconds=5,
        )
        self.assertEqual(second.fencing_token, current.fencing_token)


if __name__ == "__main__":
    unittest.main()
