from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from trading_harness.learning_ledger import LearningLedger
from trading_harness.staging_inbox import (
    NON_AUTHORITATIVE_STAGING,
    STAGING_INBOX_SCHEMA_VERSION,
    StageTradeRequest,
    StagingConflict,
    StagingDecision,
    StagingEventType,
    StagingNotFound,
    StagingState,
    StagingStorageError,
    StagingValidationError,
    TradeStagingInbox,
    TrustedQuoteDecision,
    TrustedQuoteRequest,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "trading_harness" / "staging_inbox.py"
NOW = datetime(2026, 8, 25, 12, 0, 0, 123456, tzinfo=timezone.utc)
ANALYSIS_A = "a" * 64
ANALYSIS_B = "b" * 64


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def staged_quote(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
    return TrustedQuoteDecision.staged(
        analysis_hash=request.expected_analysis_hash,
        ticket_payload={
            "schema_version": "risk_ticket.v1",
            "ticket_id": "TEST-TICKET-1",
            "assessment_hash": request.expected_analysis_hash,
            "side": "buy",
            "quantity": "0.01",
            "entry_bound": "3000",
            "stop_price": "2900",
            "approval_created": False,
            "eligible_to_trade": False,
            "order_submitted": False,
        },
    )


class StagingInboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "staging.sqlite"
        self.clock = MutableClock()
        self.callback_count = 0

        def callback(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
            self.callback_count += 1
            return staged_quote(request)

        self.callback = callback
        self.inbox = TradeStagingInbox(
            self.path,
            quote_callback=self.callback,
            clock=self.clock,
            staged_ttl=timedelta(minutes=15),
            blocked_ttl=timedelta(minutes=2),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def request(
        *,
        asset_id: str = "eth-4h",
        analysis_hash: str = ANALYSIS_A,
        idempotency_key: str = "codex-stage-0001",
    ) -> dict[str, str]:
        return {
            "asset_id": asset_id,
            "expected_analysis_hash": analysis_hash,
            "idempotency_key": idempotency_key,
        }


class RequestBoundaryTests(StagingInboxTestCase):
    def test_untrusted_request_has_exactly_three_scalar_fields(self) -> None:
        parsed = StageTradeRequest.from_untrusted(self.request())
        self.assertEqual(
            ("asset_id", "expected_analysis_hash", "idempotency_key"),
            tuple(field.name for field in fields(StageTradeRequest)),
        )
        self.assertEqual("eth-4h", parsed.asset_id)
        self.assertEqual(ANALYSIS_A, parsed.expected_analysis_hash)

        signature = inspect.signature(TradeStagingInbox.stage)
        self.assertEqual(("self", "request"), tuple(signature.parameters))

    def test_every_extra_economic_account_approval_or_secret_field_is_rejected(self) -> None:
        forbidden = {
            "side": "buy",
            "quantity": "1",
            "price": "3000",
            "stop_loss": "2900",
            "risk_budget": "50",
            "account_id": "testnet-account",
            "network": "testnet",
            "approval": "approve it",
            "approval_token": "token",
            "credential": "secret",
            "private_key": "0xdeadbeef",
            "api_key": "secret",
            "signature": "signed",
            "nonce": 1,
            "ticket_payload": {"side": "buy"},
        }
        for name, value in forbidden.items():
            with self.subTest(name=name):
                request = self.request()
                request[name] = value  # type: ignore[assignment]
                with self.assertRaisesRegex(
                    StagingValidationError, "fields must be exactly"
                ):
                    self.inbox.stage(request)
        self.assertEqual(0, self.callback_count)

    def test_malformed_core_fields_fail_before_trusted_callback(self) -> None:
        malformed = (
            {**self.request(), "asset_id": " ETH"},
            {**self.request(), "expected_analysis_hash": "A" * 64},
            {**self.request(), "idempotency_key": {"price": "1"}},
            {"asset_id": "eth-4h", "expected_analysis_hash": ANALYSIS_A},
        )
        for request in malformed:
            with self.subTest(request=request):
                with self.assertRaises(StagingValidationError):
                    self.inbox.stage(request)
        self.assertEqual(0, self.callback_count)

    def test_callback_receives_no_idempotency_or_economic_fields(self) -> None:
        observed: list[TrustedQuoteRequest] = []

        def callback(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
            observed.append(request)
            return staged_quote(request)

        inbox = TradeStagingInbox(
            Path(self.temporary.name) / "callback.sqlite",
            quote_callback=callback,
            clock=self.clock,
        )
        inbox.stage(self.request())
        self.assertEqual(1, len(observed))
        self.assertEqual(
            ("asset_id", "expected_analysis_hash"),
            tuple(field.name for field in fields(TrustedQuoteRequest)),
        )
        self.assertFalse(hasattr(observed[0], "idempotency_key"))
        self.assertFalse(hasattr(observed[0], "side"))
        self.assertFalse(hasattr(observed[0], "account_id"))


class PersistenceAndAuthorityTests(StagingInboxTestCase):
    def test_only_callback_ticket_is_staged_and_every_authority_flag_is_false(self) -> None:
        view = self.inbox.stage(self.request())
        self.assertIs(StagingState.STAGED, view.state)
        self.assertIs(StagingDecision.STAGED, view.document.decision)
        self.assertEqual("TEST-TICKET-1", view.document.ticket_payload["ticket_id"])
        self.assertEqual(ANALYSIS_A, view.document.expected_analysis_hash)
        self.assertEqual(64, len(view.document.ticket_payload_hash))
        self.assertFalse(view.authoritative)
        self.assertEqual(
            {False}, set(view.document.authority.as_dict().values())
        )
        self.assertEqual(NON_AUTHORITATIVE_STAGING, self.inbox.authority)
        self.assertNotIn("codex-stage-0001", str(view.document.as_dict()))

        connection = sqlite3.connect(self.path)
        try:
            stored = "\n".join(
                str(value)
                for row in connection.execute(
                    "SELECT * FROM staging_documents"
                ).fetchall()
                for value in row
            )
        finally:
            connection.close()
        self.assertNotIn("codex-stage-0001", stored)

    def test_callback_block_is_immutable_and_carries_no_ticket(self) -> None:
        inbox = TradeStagingInbox(
            Path(self.temporary.name) / "blocked.sqlite",
            quote_callback=lambda request: TrustedQuoteDecision.blocked(
                block_code="nothing_to_trade",
                analysis_hash=request.expected_analysis_hash,
            ),
            clock=self.clock,
        )
        view = inbox.stage(self.request())
        self.assertIs(StagingState.BLOCKED, view.state)
        self.assertEqual("nothing_to_trade", view.document.block_code)
        self.assertIsNone(view.document.ticket_payload)
        self.assertIsNone(view.document.ticket_payload_hash)
        self.assertEqual({False}, set(view.document.authority.as_dict().values()))

    def test_bad_callback_output_fails_closed_as_a_durable_block(self) -> None:
        cases = (
            (
                "invalid",
                lambda request: {"ticket_payload": {"side": "buy"}},
                "trusted_quote_invalid",
            ),
            (
                "exception",
                lambda request: (_ for _ in ()).throw(RuntimeError("boom")),
                "trusted_quote_unavailable",
            ),
            (
                "wrong-hash",
                lambda request: TrustedQuoteDecision.staged(
                    analysis_hash=ANALYSIS_B,
                    ticket_payload={"ticket_id": "wrong-analysis"},
                ),
                "analysis_hash_mismatch",
            ),
        )
        for index, (name, callback, code) in enumerate(cases):
            with self.subTest(name=name):
                inbox = TradeStagingInbox(
                    Path(self.temporary.name) / f"{name}.sqlite",
                    quote_callback=callback,
                    clock=self.clock,
                )
                view = inbox.stage(
                    self.request(idempotency_key=f"codex-bad-{index:04d}")
                )
                self.assertIs(StagingState.BLOCKED, view.state)
                self.assertEqual(code, view.document.block_code)
                self.assertIsNone(view.document.ticket_payload)

    def test_noncanonical_callback_ticket_never_becomes_a_ticket(self) -> None:
        def callback(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
            return TrustedQuoteDecision.staged(
                analysis_hash=request.expected_analysis_hash,
                ticket_payload={"binary_float": 1.25},
            )

        inbox = TradeStagingInbox(
            Path(self.temporary.name) / "float.sqlite",
            quote_callback=callback,
            clock=self.clock,
        )
        view = inbox.stage(self.request())
        self.assertIs(StagingState.BLOCKED, view.state)
        self.assertEqual("trusted_quote_unavailable", view.document.block_code)
        self.assertIsNone(view.document.ticket_payload)

    def test_returned_ticket_mutation_cannot_change_persisted_document(self) -> None:
        first = self.inbox.stage(self.request())
        first.document.ticket_payload["side"] = "sell"
        first.document.ticket_payload["nested"] = {"tampered": True}
        restarted = TradeStagingInbox(
            self.path,
            quote_callback=lambda request: (_ for _ in ()).throw(
                AssertionError("callback must not run on restart")
            ),
            clock=self.clock,
        )
        loaded = restarted.get(first.document.document_id)
        self.assertEqual("buy", loaded.document.ticket_payload["side"])
        self.assertNotIn("nested", loaded.document.ticket_payload)
        self.assertEqual(first.document.document_hash, loaded.document.document_hash)

    def test_database_triggers_prevent_document_and_event_mutation(self) -> None:
        self.inbox.stage(self.request())
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE staging_documents SET asset_id = 'btc-4h'"
                )
            connection.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM staging_events")
        finally:
            connection.close()


class IdempotencyAndConcurrencyTests(StagingInboxTestCase):
    def test_same_request_is_idempotent_across_restart(self) -> None:
        first = self.inbox.stage(self.request())
        duplicate = self.inbox.stage(self.request())
        self.assertEqual(first, duplicate)
        self.assertEqual(1, self.callback_count)

        def forbidden_callback(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
            raise AssertionError("idempotent restart must not quote again")

        restarted = TradeStagingInbox(
            self.path,
            quote_callback=forbidden_callback,
            clock=self.clock,
        )
        loaded = restarted.stage(self.request())
        self.assertEqual(first, loaded)
        self.assertEqual(1, len(restarted.list_events()))

    def test_reusing_idempotency_key_for_different_request_conflicts(self) -> None:
        self.inbox.stage(self.request())
        with self.assertRaises(StagingConflict):
            self.inbox.stage(self.request(asset_id="btc-4h"))
        with self.assertRaises(StagingConflict):
            self.inbox.stage(self.request(analysis_hash=ANALYSIS_B))
        self.assertEqual(1, self.callback_count)

    def test_concurrent_staging_calls_quote_and_persist_exactly_once(self) -> None:
        count = 0
        count_lock = threading.Lock()
        start = threading.Barrier(9)

        def callback(request: TrustedQuoteRequest) -> TrustedQuoteDecision:
            nonlocal count
            with count_lock:
                count += 1
            time.sleep(0.05)
            return staged_quote(request)

        inboxes = [
            TradeStagingInbox(
                self.path,
                quote_callback=callback,
                clock=self.clock,
                busy_timeout_ms=10_000,
            )
            for _ in range(8)
        ]
        identifiers: list[str] = []
        failures: list[BaseException] = []

        def worker(inbox: TradeStagingInbox) -> None:
            try:
                start.wait()
                identifiers.append(inbox.stage(self.request()).document.document_id)
            except BaseException as error:  # Collected and asserted in the main thread.
                failures.append(error)

        threads = [threading.Thread(target=worker, args=(inbox,)) for inbox in inboxes]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(1, count)
        self.assertEqual(8, len(identifiers))
        self.assertEqual(1, len(set(identifiers)))
        self.assertEqual(1, len(self.inbox.list_documents()))
        self.assertEqual(1, len(self.inbox.list_events()))


class ExpiryAndReadApiTests(StagingInboxTestCase):
    def test_get_expires_requested_document_beyond_background_batch(self) -> None:
        created = [
            self.inbox.stage(
                self.request(
                    asset_id=f"batched-asset-{index}",
                    idempotency_key=f"batched-stage-{index}",
                )
            )
            for index in range(33)
        ]
        self.clock.value = NOW + timedelta(minutes=16)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            first_batch = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT document_id FROM staging_documents
                    ORDER BY expires_at, document_id LIMIT 32
                    """
                )
            }
        omitted = next(
            view for view in created if view.document.document_id not in first_batch
        )

        result = self.inbox.get(omitted.document.document_id)

        self.assertIs(StagingState.EXPIRED, result.state)

    def test_expiry_is_one_append_only_event_and_survives_restart(self) -> None:
        created = self.inbox.stage(self.request())
        self.assertIs(StagingState.STAGED, created.state)
        self.clock.value = NOW + timedelta(minutes=16)

        expired = self.inbox.get(created.document.document_id)
        self.assertIs(StagingState.EXPIRED, expired.state)
        self.assertEqual(self.clock.value, expired.expired_at)
        self.assertEqual(2, expired.latest_event_sequence)
        self.assertEqual(
            (
                StagingEventType.DOCUMENT_CREATED,
                StagingEventType.DOCUMENT_EXPIRED,
            ),
            tuple(event.event_type for event in self.inbox.list_events()),
        )
        self.assertEqual(0, self.inbox.expire_due())

        restarted = TradeStagingInbox(
            self.path,
            quote_callback=self.callback,
            clock=self.clock,
        )
        self.assertIs(
            StagingState.EXPIRED,
            restarted.get_by_idempotency_key("codex-stage-0001").state,
        )
        self.assertEqual(2, len(restarted.list_events()))

    def test_blocked_documents_use_shorter_configured_expiry(self) -> None:
        inbox = TradeStagingInbox(
            Path(self.temporary.name) / "blocked-expiry.sqlite",
            quote_callback=lambda request: TrustedQuoteDecision.blocked(
                block_code="nothing_to_trade"
            ),
            clock=self.clock,
            staged_ttl=timedelta(minutes=15),
            blocked_ttl=timedelta(minutes=2),
        )
        blocked = inbox.stage(self.request())
        self.assertEqual(NOW + timedelta(minutes=2), blocked.document.expires_at)
        self.clock.value = NOW + timedelta(minutes=3)
        self.assertIs(StagingState.EXPIRED, inbox.get(blocked.document.document_id).state)

    def test_bounded_read_apis_filter_state_and_report_missing_records(self) -> None:
        first = self.inbox.stage(self.request())
        second = self.inbox.stage(
            self.request(asset_id="btc-4h", idempotency_key="codex-stage-0002")
        )
        self.assertEqual(
            {first.document.document_id, second.document.document_id},
            {view.document.document_id for view in self.inbox.list_documents()},
        )
        self.assertEqual(1, len(self.inbox.list_documents(limit=1)))
        self.assertEqual(2, len(self.inbox.list_documents(state="staged")))
        self.assertEqual(2, len(self.inbox.list_events(after_sequence=0)))
        self.assertEqual(1, len(self.inbox.list_events(after_sequence=1)))
        with self.assertRaises(StagingNotFound):
            self.inbox.get("stg_" + "f" * 64)
        with self.assertRaises(StagingNotFound):
            self.inbox.get_by_idempotency_key("unknown-key")
        with self.assertRaises(StagingValidationError):
            self.inbox.list_documents(limit=0)
        with self.assertRaises(StagingValidationError):
            self.inbox.list_events(after_sequence=-1)


class IntegrityAndArchitectureTests(StagingInboxTestCase):
    def test_live_staging_stops_before_crossing_shared_state_limit(self) -> None:
        blocked = False
        limit = 192 * 1024
        with (
            patch("trading_harness.staging_inbox.MAX_SHARED_STATE_FILE_BYTES", limit),
            patch(
                "trading_harness.staging_inbox._STAGING_WRITE_HEADROOM_BYTES",
                32 * 1024,
            ),
        ):
            for index in range(128):
                request = self.request(
                    asset_id=f"bounded-asset-{index}",
                    idempotency_key=f"bounded-stage-{index}",
                )
                try:
                    self.inbox.stage(request)
                except StagingStorageError:
                    blocked = True
                    break

        self.assertTrue(blocked)
        for path in (self.path, Path(f"{self.path}-wal")):
            if path.exists():
                self.assertLessEqual(path.stat().st_size, limit)

    def test_existing_only_rejects_invalid_stores_without_mutating_main_file(self) -> None:
        zero_byte = Path(self.temporary.name) / "zero-byte.sqlite"
        zero_byte.touch()

        schema_less = Path(self.temporary.name) / "schema-less.sqlite"
        connection = sqlite3.connect(schema_less)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

        wrong_store = Path(self.temporary.name) / "learning-store.sqlite"
        LearningLedger(wrong_store)

        for path in (zero_byte, schema_less, wrong_store):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                before_modified = path.stat().st_mtime_ns
                with self.assertRaises(StagingStorageError):
                    TradeStagingInbox(
                        path,
                        quote_callback=self.callback,
                        clock=self.clock,
                        must_exist=True,
                    )
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(before_modified, path.stat().st_mtime_ns)

    def test_existing_only_does_not_repair_schema_or_migrations(self) -> None:
        cases = (
            (
                "migration",
                "DELETE FROM staging_schema_migrations WHERE version = 1",
                "SELECT count(*) FROM staging_schema_migrations",
            ),
            (
                "index",
                "DROP INDEX idx_staging_documents_created",
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_staging_documents_created'",
            ),
            (
                "trigger",
                "DROP TRIGGER staging_events_no_delete",
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'staging_events_no_delete'",
            ),
        )
        for name, mutation, absent_query in cases:
            with self.subTest(name=name):
                path = Path(self.temporary.name) / f"missing-{name}.sqlite"
                TradeStagingInbox(
                    path,
                    quote_callback=self.callback,
                    clock=self.clock,
                )
                connection = sqlite3.connect(path)
                try:
                    connection.execute(mutation)
                    connection.commit()
                finally:
                    connection.close()
                before = path.read_bytes()

                with self.assertRaises(StagingStorageError):
                    TradeStagingInbox(
                        path,
                        quote_callback=self.callback,
                        clock=self.clock,
                        must_exist=True,
                    )

                self.assertEqual(before, path.read_bytes())
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    self.assertEqual(0, connection.execute(absent_query).fetchone()[0])
                finally:
                    connection.close()

    def test_existing_only_valid_reopen_keeps_later_operations_writable(self) -> None:
        reopened = TradeStagingInbox(
            self.path,
            quote_callback=self.callback,
            clock=self.clock,
            must_exist=True,
        )
        view = reopened.stage(self.request())
        self.assertEqual((view,), reopened.list_documents())

    def test_existing_only_verification_includes_a_retained_wal(self) -> None:
        path = Path(self.temporary.name) / "retained-wal.sqlite"
        keeper = sqlite3.connect(path)
        try:
            self.assertEqual(
                "wal", keeper.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            )
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("BEGIN")
            keeper.execute("SELECT count(*) FROM sqlite_master").fetchone()
            inbox = TradeStagingInbox(
                path,
                quote_callback=self.callback,
                clock=self.clock,
            )
            expected = inbox.stage(self.request(idempotency_key="retained-wal-stage"))
            wal_path = Path(f"{path}-wal")
            self.assertTrue(wal_path.is_file())
            self.assertGreater(wal_path.stat().st_size, 0)
            main_before = path.read_bytes()
            wal_before = wal_path.read_bytes()

            reopened = TradeStagingInbox(
                path,
                quote_callback=self.callback,
                clock=self.clock,
                must_exist=True,
            )

            self.assertEqual(main_before, path.read_bytes())
            self.assertEqual(wal_before, wal_path.read_bytes())
            self.assertEqual((expected,), reopened.list_documents())
        finally:
            keeper.close()

    def test_existing_only_rejects_sidecar_symlinks_without_touching_target(self) -> None:
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = Path(self.temporary.name) / f"symlink{suffix}.sqlite"
                TradeStagingInbox(
                    path,
                    quote_callback=self.callback,
                    clock=self.clock,
                )
                main_before = path.read_bytes()
                target = Path(self.temporary.name) / f"target{suffix}"
                target.write_bytes(b"do-not-touch")
                sidecar = Path(f"{path}{suffix}")
                sidecar.unlink(missing_ok=True)
                sidecar.symlink_to(target)

                with self.assertRaisesRegex(StagingStorageError, "regular file"):
                    TradeStagingInbox(
                        path,
                        quote_callback=self.callback,
                        clock=self.clock,
                        must_exist=True,
                    )

                self.assertEqual(b"do-not-touch", target.read_bytes())
                self.assertEqual(main_before, path.read_bytes())

    def test_schema_is_checksummed_restartable_and_owns_only_staging_tables(self) -> None:
        self.inbox.stage(self.request())
        restarted = TradeStagingInbox(
            self.path,
            quote_callback=self.callback,
            clock=self.clock,
        )
        self.assertEqual(64, len(restarted.verify_integrity()))
        connection = sqlite3.connect(self.path)
        try:
            migrations = connection.execute(
                "SELECT version, name, checksum FROM staging_schema_migrations"
            ).fetchall()
            staging_tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name LIKE 'staging_%'
                    """
                )
            }
        finally:
            connection.close()
        self.assertEqual(STAGING_INBOX_SCHEMA_VERSION, migrations[-1][0])
        self.assertEqual(64, len(migrations[-1][2]))
        self.assertEqual(
            {
                "staging_schema_migrations",
                "staging_documents",
                "staging_events",
            },
            staging_tables,
        )

    def test_migration_or_payload_tampering_fails_closed(self) -> None:
        migration_path = Path(self.temporary.name) / "migration.sqlite"
        TradeStagingInbox(
            migration_path,
            quote_callback=self.callback,
            clock=self.clock,
        )
        connection = sqlite3.connect(migration_path)
        try:
            connection.execute(
                "UPDATE staging_schema_migrations SET checksum = ? WHERE version = 1",
                (ANALYSIS_A,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StagingStorageError):
            TradeStagingInbox(
                migration_path,
                quote_callback=self.callback,
                clock=self.clock,
            )

        self.inbox.stage(self.request())
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER staging_documents_no_update")
            connection.execute(
                "UPDATE staging_documents SET payload_hash = ?", (ANALYSIS_A,)
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StagingStorageError):
            TradeStagingInbox(
                self.path,
                quote_callback=self.callback,
                clock=self.clock,
            )

    def test_hash_chain_tampering_fails_even_if_trigger_is_restored(self) -> None:
        self.inbox.stage(self.request())
        self.inbox.stage(
            self.request(asset_id="btc-4h", idempotency_key="codex-stage-0002")
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER staging_events_no_update")
            connection.execute(
                "UPDATE staging_events SET chain_hash = ? WHERE sequence = 1",
                ("f" * 64,),
            )
            connection.execute(
                """
                CREATE TRIGGER staging_events_no_update
                BEFORE UPDATE ON staging_events
                BEGIN SELECT RAISE(ABORT, 'staging events are append-only'); END
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(StagingStorageError, "chain"):
            before = self.path.read_bytes()
            TradeStagingInbox(
                self.path,
                quote_callback=self.callback,
                clock=self.clock,
                must_exist=True,
            )
        self.assertEqual(before, self.path.read_bytes())

    def test_static_import_and_callable_capability_boundary(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden_dependencies = {
            "approval",
            "execution_store",
            "credential_provider",
            "hyperliquid_signer",
            "dispatcher",
            "hyperliquid_transport",
        }
        self.assertFalse(
            any(
                dependency in imported_name
                for dependency in forbidden_dependencies
                for imported_name in imported
            ),
            imported,
        )
        public_callables = {
            name
            for name in dir(TradeStagingInbox)
            if not name.startswith("_")
            and callable(getattr(TradeStagingInbox, name, None))
        }
        self.assertTrue(
            {
                "stage",
                "expire_due",
                "get",
                "get_by_idempotency_key",
                "list_documents",
                "list_events",
                "verify_integrity",
            }.issubset(public_callables)
        )
        self.assertTrue(
            {
                "approve",
                "reserve_risk",
                "load_credentials",
                "sign",
                "submit",
                "execute",
                "dispatch",
                "send_order",
            }.isdisjoint(public_callables)
        )
        self.assertEqual({False}, set(self.inbox.authority.as_dict().values()))


if __name__ == "__main__":
    unittest.main()
