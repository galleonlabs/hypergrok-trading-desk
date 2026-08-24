from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from trading_harness.errors import RecordNotFound, StateConflict, StorageError, ValidationError
from trading_harness.research_store import ResearchStore
from trading_harness.shadow import (
    ShadowLedger,
    ShadowRecordStatus,
    ShadowVariant,
)
from trading_harness.shadow_store import SHADOW_STORE_SCHEMA_VERSION, ShadowStore
from tests.test_shadow import (
    START,
    digest,
    drift,
    outcome_record,
    protocol,
    signal_record,
)


class ShadowStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "shadow.sqlite3"
        self.store = ShadowStore(self.path)
        self.study = protocol()
        self.store.register_protocol(self.study, stored_at=START)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def append_pair(self, index: int, *, store: ShadowStore | None = None) -> tuple[object, object]:
        target = self.store if store is None else store
        ta = signal_record(self.study, index, ShadowVariant.TA_ONLY)
        sentiment = signal_record(self.study, index, ShadowVariant.TA_SENTIMENT)
        target.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        target.append_signal(
            self.study.protocol_hash,
            sentiment,
            appended_at=sentiment.recorded_at,
        )
        ta_outcome = outcome_record(ta, index, net_r=Decimal("0.1"))
        sentiment_outcome = outcome_record(
            sentiment, index, net_r=Decimal("0.2")
        )
        target.append_outcome(
            self.study.protocol_hash,
            ta_outcome,
            appended_at=ta_outcome.recorded_at,
        )
        target.append_outcome(
            self.study.protocol_hash,
            sentiment_outcome,
            appended_at=sentiment_outcome.recorded_at,
        )
        return ta, sentiment


class MigrationAndProtocolTests(ShadowStoreTestCase):
    def test_schema_is_checksummed_wal_namespaced_and_coexists(self) -> None:
        combined = Path(self.temporary.name) / "combined.sqlite3"
        ResearchStore(combined)
        ShadowStore(combined)
        connection = sqlite3.connect(combined)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            migration = connection.execute(
                "SELECT version, name, checksum FROM shadow_schema_migrations"
            ).fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(mode, "wal")
        self.assertEqual(migration[0], SHADOW_STORE_SCHEMA_VERSION)
        self.assertEqual(len(migration[2]), 64)
        self.assertIn("shadow_protocols", tables)
        self.assertIn("shadow_events", tables)
        self.assertIn("shadow_validation_artifacts", tables)
        self.assertIn("research_tracked_assets", tables)

    def test_store_is_file_backed_and_restart_is_idempotent(self) -> None:
        with self.assertRaisesRegex(ValidationError, "file-backed"):
            ShadowStore(":memory:")
        restarted = ShadowStore(self.path)
        self.assertEqual(restarted.get_protocol(self.study.protocol_hash), self.study)
        self.assertEqual(restarted.register_protocol(self.study), self.study)

    def test_protocol_must_be_durable_before_start_and_identity_is_immutable(self) -> None:
        later = replace(
            self.study,
            protocol_id="another-id",
            minimum_incremental_r=Decimal("0.1"),
        )
        with self.assertRaisesRegex(ValidationError, "before shadow start"):
            ShadowStore(Path(self.temporary.name) / "late.sqlite3").register_protocol(
                later, stored_at=later.started_at + timedelta(microseconds=1)
            )

        conflicting = replace(
            self.study,
            minimum_incremental_r=Decimal("0.1"),
        )
        with self.assertRaisesRegex(StateConflict, "identity"):
            self.store.register_protocol(conflicting, stored_at=START)

    def test_tampered_migration_or_missing_trigger_fails_restart(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE shadow_schema_migrations SET checksum = ? WHERE version = 1",
                (digest(999),),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StorageError, "checksum"):
            ShadowStore(self.path)


class AppendAndRestartTests(ShadowStoreTestCase):
    def test_pending_and_closed_events_survive_restart_with_exact_chain(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        ledger = self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        self.assertIs(ledger.status_for(ta.signal_id), ShadowRecordStatus.PENDING)

        restarted = ShadowStore(self.path)
        loaded = restarted.load_ledger(self.study.protocol_hash)
        self.assertEqual(loaded, ledger)
        outcome = outcome_record(ta, 0)
        closed = restarted.append_outcome(
            self.study.protocol_hash,
            outcome,
            appended_at=outcome.recorded_at,
        )
        self.assertIs(closed.status_for(ta.signal_id), ShadowRecordStatus.CLOSED)
        self.assertEqual(ShadowStore(self.path).load_ledger(self.study.protocol_hash), closed)

        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                """
                SELECT sequence, previous_chain_hash, chain_hash
                FROM shadow_events ORDER BY sequence
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], [0, 1])
        self.assertEqual(
            rows[0][1], ShadowLedger.create(self.study).chain_hash
        )
        self.assertEqual(rows[1][1], rows[0][2])
        self.assertEqual(rows[-1][2], closed.chain_hash)

    def test_paired_variants_are_allowed_but_duplicate_variant_is_not(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        sentiment = signal_record(self.study, 0, ShadowVariant.TA_SENTIMENT)
        self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        self.store.append_signal(
            self.study.protocol_hash,
            sentiment,
            appended_at=sentiment.recorded_at,
        )
        ledger = self.store.load_ledger(self.study.protocol_hash)
        self.assertEqual(len(ledger.events), 2)
        self.assertEqual(ta.comparison_id, sentiment.comparison_id)

        duplicate = replace(
            ta,
            event_id="different-event",
            signal_id="different-signal",
            signal_hash=digest(999),
            evidence_hash=digest(998),
        )
        with self.assertRaisesRegex(StateConflict, "comparison"):
            self.store.append_signal(
                self.study.protocol_hash,
                duplicate,
                appended_at=duplicate.recorded_at,
            )

    def test_outcome_without_prior_signal_or_with_changed_binding_is_rejected(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        outcome = outcome_record(ta, 0)
        with self.assertRaisesRegex(StateConflict, "no prior signal"):
            self.store.append_outcome(
                self.study.protocol_hash,
                outcome,
                appended_at=outcome.recorded_at,
            )
        self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        changed = replace(outcome, data_hash=digest(997))
        with self.assertRaisesRegex(StateConflict, "exact signal evidence"):
            self.store.append_outcome(
                self.study.protocol_hash,
                changed,
                appended_at=changed.recorded_at,
            )
        self.assertEqual(
            len(self.store.load_ledger(self.study.protocol_hash).events), 1
        )

    def test_future_late_and_backdated_appends_fail_closed(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        with self.assertRaisesRegex(ValidationError, "future evidence"):
            self.store.append_signal(
                self.study.protocol_hash,
                ta,
                appended_at=ta.recorded_at - timedelta(microseconds=1),
            )
        with self.assertRaisesRegex(ValidationError, "after its expiry"):
            self.store.append_signal(
                self.study.protocol_hash,
                ta,
                appended_at=ta.expires_at + timedelta(microseconds=1),
            )
        self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.expires_at
        )
        counterpart = signal_record(self.study, 0, ShadowVariant.TA_SENTIMENT)
        with self.assertRaisesRegex(StateConflict, "cannot move backwards"):
            self.store.append_signal(
                self.study.protocol_hash,
                counterpart,
                appended_at=counterpart.recorded_at,
            )

    def test_concurrent_duplicate_append_has_exactly_one_winner(self) -> None:
        signal = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        first = ShadowStore(self.path, busy_timeout_ms=30_000)
        second = ShadowStore(self.path, busy_timeout_ms=30_000)
        barrier = threading.Barrier(2)

        def worker(store: ShadowStore) -> str:
            barrier.wait()
            try:
                store.append_signal(
                    self.study.protocol_hash,
                    signal,
                    appended_at=signal.recorded_at,
                )
                return "won"
            except StateConflict:
                return "lost"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(worker, (first, second)))
        self.assertEqual(sorted(results), ["lost", "won"])
        self.assertEqual(
            len(ShadowStore(self.path).load_ledger(self.study.protocol_hash).events),
            1,
        )


class ArtifactAndTamperTests(ShadowStoreTestCase):
    def test_artifact_is_created_only_by_evaluation_and_survives_restart(self) -> None:
        self.append_pair(0)
        as_of = START + timedelta(days=90)
        artifact = self.store.evaluate_and_store(
            self.study.protocol_hash,
            drift(self.study, as_of),
            as_of=as_of,
            stored_at=as_of,
        )
        self.assertFalse(hasattr(self.store, "put_validation_artifact"))
        self.assertFalse(hasattr(self.store, "delete_validation_artifact"))
        loaded = ShadowStore(self.path).get_validation_artifact(
            artifact.artifact_hash
        )
        self.assertEqual(loaded, artifact)
        self.assertEqual(loaded.artifact_hash, artifact.artifact_hash)
        again = self.store.evaluate_and_store(
            self.study.protocol_hash,
            drift(self.study, as_of),
            as_of=as_of,
            stored_at=as_of + timedelta(seconds=1),
        )
        self.assertEqual(again, artifact)

    def test_as_of_uses_only_events_durable_by_that_time(self) -> None:
        self.append_pair(0)
        as_of = START + timedelta(days=1)
        first = self.store.evaluate_and_store(
            self.study.protocol_hash,
            drift(self.study, as_of),
            as_of=as_of,
            stored_at=as_of,
        )
        self.append_pair(2)
        second = self.store.evaluate_and_store(
            self.study.protocol_hash,
            drift(self.study, as_of),
            as_of=as_of,
            stored_at=START + timedelta(days=3),
        )
        self.assertEqual(first.artifact_hash, second.artifact_hash)
        self.assertEqual(first.sentiment_metrics.trade_count, 1)

    def test_database_triggers_forbid_update_and_delete(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE shadow_events SET payload_json = '{}' WHERE sequence = 0"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM shadow_events WHERE sequence = 0")
            connection.rollback()
        finally:
            connection.close()

    def test_artifact_payload_tamper_is_detected_on_read(self) -> None:
        self.append_pair(0)
        as_of = START + timedelta(days=90)
        artifact = self.store.evaluate_and_store(
            self.study.protocol_hash,
            drift(self.study, as_of),
            as_of=as_of,
            stored_at=as_of,
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER shadow_artifacts_no_update")
            connection.execute(
                """
                UPDATE shadow_validation_artifacts
                SET payload_json = '{}' WHERE artifact_hash = ?
                """,
                (artifact.artifact_hash,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StorageError, "payload hash"):
            self.store.get_validation_artifact(artifact.artifact_hash)

    def test_chain_tamper_is_detected_before_another_append(self) -> None:
        ta = signal_record(self.study, 0, ShadowVariant.TA_ONLY)
        self.store.append_signal(
            self.study.protocol_hash, ta, appended_at=ta.recorded_at
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER shadow_events_no_update")
            connection.execute(
                "UPDATE shadow_events SET chain_hash = ? WHERE sequence = 0",
                (digest(777),),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StorageError, "chain hash"):
            self.store.load_ledger(self.study.protocol_hash)
        second = signal_record(self.study, 1, ShadowVariant.TA_ONLY)
        with self.assertRaises(StorageError):
            self.store.append_signal(
                self.study.protocol_hash,
                second,
                appended_at=second.recorded_at,
            )

    def test_missing_records_and_mutation_methods_are_absent(self) -> None:
        with self.assertRaises(RecordNotFound):
            self.store.get_protocol(digest(123456))
        with self.assertRaises(RecordNotFound):
            self.store.get_validation_artifact(digest(123457))
        for name in (
            "delete_protocol",
            "update_protocol",
            "delete_event",
            "update_event",
            "rewrite_event",
        ):
            self.assertFalse(hasattr(self.store, name))


if __name__ == "__main__":
    unittest.main()
