from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.daily_loss import (
    DailyLossBinding,
    DailyLossLedger,
    IncompleteDailyLossCoverage,
    LossCoverageSource,
)
from trading_harness.domain import Environment
from trading_harness.errors import StateConflict, StorageError, ValidationError
from trading_harness.executor_config import ExecutorConfigDrift


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def binding(**changes: object) -> DailyLossBinding:
    values: dict[str, object] = {
        "account_id": "dedicated-testnet",
        "environment": Environment.TESTNET,
        "config_hash": digest("config-v1"),
        "daily_loss_limit": Decimal("25"),
        "settlement_currency": "USDC",
    }
    values.update(changes)
    return DailyLossBinding(**values)  # type: ignore[arg-type]


class DailyLossLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "executor.sqlite3"
        self.clock = FakeClock(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc))
        self.ledger = DailyLossLedger(
            self.database.absolute(), binding=binding(), clock=self.clock
        )

    def cover_current_day(self) -> None:
        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertTrue(
            self.ledger.record_coverage(
                coverage_id=f"fills-{self.clock.now:%Y%m%d-%H%M}",
                source=LossCoverageSource.FILLS,
                covered_from=start,
                covered_through=self.clock.now,
                source_cursor_hash=digest(f"fills-{self.clock.now.isoformat()}"),
            )
        )
        self.assertTrue(
            self.ledger.record_coverage(
                coverage_id=f"funding-{self.clock.now:%Y%m%d-%H%M}",
                source=LossCoverageSource.FUNDING,
                covered_from=start,
                covered_through=self.clock.now,
                source_cursor_hash=digest(f"funding-{self.clock.now.isoformat()}"),
            )
        )

    def test_existing_only_reopen_verifies_without_mutating_the_main_file(self) -> None:
        before = self.database.read_bytes()
        query_only_values: list[int] = []
        verification_paths: list[Path] = []
        verify_integrity = DailyLossLedger._verify_integrity

        def observe_query_only(connection: sqlite3.Connection) -> None:
            query_only_values.append(connection.execute("PRAGMA query_only").fetchone()[0])
            database_path = Path(
                connection.execute("PRAGMA database_list").fetchone()[2]
            )
            verification_paths.append(database_path)
            verify_integrity(connection)

        with patch.object(
            DailyLossLedger, "_verify_integrity", side_effect=observe_query_only
        ):
            reopened = DailyLossLedger(
                self.database.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )

        self.assertEqual(query_only_values, [1])
        self.assertEqual(
            self.database.parent.resolve(),
            verification_paths[0].parent.parent.resolve(),
        )
        self.assertTrue(
            verification_paths[0].parent.name.startswith(
                ".trading-sqlite-verify-"
            )
        )
        self.assertEqual(self.database.read_bytes(), before)
        self.assertTrue(
            reopened.record_fee(
                event_id="strict-reopen-fee",
                source_ref="strict-reopen-fill",
                occurred_at=self.clock.now,
                fee=Decimal("0.01"),
            )
        )

    def test_existing_only_rejects_unexpected_loss_trigger(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER executor_deployment_binding
                BEFORE INSERT ON daily_loss_events
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
        before = self.database.read_bytes()

        with self.assertRaisesRegex(StorageError, "deployment binding schema"):
            DailyLossLedger(
                self.database.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )

        self.assertEqual(before, self.database.read_bytes())

    def test_existing_only_rejects_zero_byte_and_schema_less_files_without_repair(
        self,
    ) -> None:
        zero_byte = Path(self.temporary.name) / "zero-byte.sqlite3"
        zero_byte.touch()
        before_zero = zero_byte.read_bytes()
        with self.assertRaises(StorageError):
            DailyLossLedger(
                zero_byte.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )
        self.assertEqual(zero_byte.read_bytes(), before_zero)

        schema_less = Path(self.temporary.name) / "schema-less.sqlite3"
        with closing(sqlite3.connect(schema_less)) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
        before_schema_less = schema_less.read_bytes()
        with self.assertRaises(StorageError):
            DailyLossLedger(
                schema_less.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )
        self.assertEqual(schema_less.read_bytes(), before_schema_less)

    def test_existing_only_rejects_wrong_store_and_missing_binding_without_repair(
        self,
    ) -> None:
        wrong_store = Path(self.temporary.name) / "wrong-store.sqlite3"
        with closing(sqlite3.connect(wrong_store)) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("CREATE TABLE unrelated_state (value TEXT)")
        before_wrong = wrong_store.read_bytes()
        with self.assertRaises(StorageError):
            DailyLossLedger(
                wrong_store.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )
        self.assertEqual(wrong_store.read_bytes(), before_wrong)

        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TRIGGER daily_loss_binding_no_delete")
            connection.execute("DELETE FROM daily_loss_binding")
            connection.execute(
                """
                CREATE TRIGGER daily_loss_binding_no_delete
                BEFORE DELETE ON daily_loss_binding
                BEGIN SELECT RAISE(ABORT, 'daily-loss binding is immutable'); END
                """
            )
        before_missing = self.database.read_bytes()
        with self.assertRaisesRegex(StorageError, "binding is missing"):
            DailyLossLedger(
                self.database.absolute(),
                binding=binding(),
                clock=self.clock,
                must_exist=True,
            )
        self.assertEqual(self.database.read_bytes(), before_missing)

    def test_existing_only_rejects_binding_drift_without_mutating_the_main_file(
        self,
    ) -> None:
        before = self.database.read_bytes()

        with self.assertRaises(ExecutorConfigDrift):
            DailyLossLedger(
                self.database.absolute(),
                binding=binding(config_hash=digest("config-v2")),
                clock=self.clock,
                must_exist=True,
            )

        self.assertEqual(self.database.read_bytes(), before)

    def test_rejected_retained_wal_verification_mutates_no_source_file(self) -> None:
        keeper = sqlite3.connect(self.database)
        try:
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("BEGIN")
            keeper.execute("SELECT * FROM daily_loss_binding").fetchall()
            self.assertTrue(
                self.ledger.record_fee(
                    event_id="retained-wal-fee",
                    source_ref="retained-wal-fill",
                    occurred_at=self.clock.now,
                    fee="0.01",
                )
            )
            wal = Path(f"{self.database}-wal")
            self.assertGreater(wal.stat().st_size, 0)
            before = {
                item.name: item.read_bytes()
                for item in self.database.parent.iterdir()
                if item.is_file()
            }

            with self.assertRaises(ExecutorConfigDrift):
                DailyLossLedger(
                    self.database.absolute(),
                    binding=binding(config_hash=digest("drifted-retained-wal")),
                    clock=self.clock,
                    must_exist=True,
                )

            self.assertEqual(
                before,
                {
                    item.name: item.read_bytes()
                    for item in self.database.parent.iterdir()
                    if item.is_file()
                },
            )
        finally:
            keeper.rollback()
            keeper.close()

    def test_incomplete_source_coverage_fails_closed(self) -> None:
        with self.assertRaises(IncompleteDailyLossCoverage) as missing_both:
            self.ledger.snapshot()
        self.assertEqual(missing_both.exception.missing_sources, ("fills", "funding"))

        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.ledger.record_coverage(
            coverage_id="fills-only",
            source="fills",
            covered_from=start,
            covered_through=self.clock.now,
            source_cursor_hash=digest("fills-only"),
        )
        partial = self.ledger.snapshot(require_complete=False)
        self.assertFalse(partial.coverage_complete)
        self.assertEqual(partial.missing_sources, ("funding",))
        with self.assertRaises(IncompleteDailyLossCoverage):
            self.ledger.remaining()

    def test_latest_complete_snapshot_uses_exact_fresh_common_watermark(self) -> None:
        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        watermark = self.clock.now - timedelta(milliseconds=750)
        for source in (LossCoverageSource.FILLS, LossCoverageSource.FUNDING):
            self.ledger.record_coverage(
                coverage_id=f"fresh-watermark-{source.value}",
                source=source,
                covered_from=start,
                covered_through=watermark,
                source_cursor_hash=digest(f"fresh-watermark-{source.value}"),
            )
        with self.assertRaises(IncompleteDailyLossCoverage):
            self.ledger.snapshot()

        snapshot = self.ledger.latest_complete_snapshot(maximum_age_seconds=1)

        self.assertEqual(watermark, snapshot.as_of)
        self.assertTrue(snapshot.coverage_complete)
        self.assertEqual((), snapshot.missing_sources)

    def test_latest_complete_snapshot_never_rounds_stale_or_cross_day_coverage(self) -> None:
        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        stale = self.clock.now - timedelta(seconds=6)
        for source in (LossCoverageSource.FILLS, LossCoverageSource.FUNDING):
            self.ledger.record_coverage(
                coverage_id=f"stale-watermark-{source.value}",
                source=source,
                covered_from=start,
                covered_through=stale,
                source_cursor_hash=digest(f"stale-watermark-{source.value}"),
            )
        with self.assertRaisesRegex(StateConflict, "stale"):
            self.ledger.latest_complete_snapshot(maximum_age_seconds=5)
        with self.assertRaisesRegex(ValidationError, "current UTC day"):
            self.ledger.snapshot(as_of=start - timedelta(microseconds=1))

    def test_gaps_in_either_required_stream_fail_closed(self) -> None:
        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        gap_start = start + timedelta(hours=4)
        gap_end = gap_start + timedelta(minutes=1)
        intervals = ((start, gap_start), (gap_end, self.clock.now))
        for source in (LossCoverageSource.FILLS, LossCoverageSource.FUNDING):
            for index, (left, right) in enumerate(intervals):
                self.ledger.record_coverage(
                    coverage_id=f"{source.value}-{index}",
                    source=source,
                    covered_from=left,
                    covered_through=right,
                    source_cursor_hash=digest(f"{source.value}-{index}"),
                )
        snapshot = self.ledger.snapshot(require_complete=False)
        self.assertFalse(snapshot.coverage_complete)
        self.assertEqual(snapshot.missing_sources, ("fills", "funding"))

    def test_losses_fees_and_funding_debits_are_monotonic_and_wins_do_not_replenish(self) -> None:
        self.cover_current_day()
        occurred = self.clock.now - timedelta(minutes=1)
        self.assertTrue(
            self.ledger.record_realized_pnl(
                event_id="loss-1",
                source_ref="fill-loss-1",
                occurred_at=occurred,
                realized_pnl=Decimal("-10"),
            )
        )
        after_loss = self.ledger.snapshot()
        self.assertEqual(after_loss.used, Decimal("10"))
        self.assertEqual(after_loss.remaining, Decimal("15"))

        self.ledger.record_realized_pnl(
            event_id="win-1",
            source_ref="fill-win-1",
            occurred_at=occurred,
            realized_pnl=Decimal("100"),
        )
        after_win = self.ledger.snapshot()
        self.assertEqual(after_win.used, after_loss.used)
        self.assertEqual(after_win.remaining, after_loss.remaining)

        self.ledger.record_fee(
            event_id="fee-1",
            source_ref="fill-fee-1",
            occurred_at=occurred,
            fee=Decimal("2.50"),
        )
        self.ledger.record_funding(
            event_id="funding-paid",
            source_ref="funding-1",
            occurred_at=occurred,
            net_funding=Decimal("-1.25"),
        )
        debited = self.ledger.snapshot()
        self.assertEqual(debited.realized_loss_debit, Decimal("10"))
        self.assertEqual(debited.fee_debit, Decimal("2.5"))
        self.assertEqual(debited.funding_debit, Decimal("1.25"))
        self.assertEqual(debited.used, Decimal("13.75"))
        self.assertEqual(debited.remaining, Decimal("11.25"))

        self.ledger.record_funding(
            event_id="funding-received",
            source_ref="funding-2",
            occurred_at=occurred,
            net_funding=Decimal("50"),
        )
        final = self.ledger.snapshot()
        self.assertEqual(final.used, debited.used)
        self.assertEqual(final.remaining, debited.remaining)
        self.assertEqual(final.event_count, 5)

    def test_budget_floors_at_zero_without_losing_exact_overrun(self) -> None:
        self.cover_current_day()
        self.ledger.record_realized_pnl(
            event_id="large-loss",
            source_ref="fill-large-loss",
            occurred_at=self.clock.now,
            realized_pnl=Decimal("-30.125"),
        )
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.used, Decimal("30.125"))
        self.assertEqual(snapshot.remaining, Decimal("0"))

    def test_exact_duplicates_are_idempotent_and_conflicts_are_rejected(self) -> None:
        values = {
            "event_id": "loss-duplicate",
            "source_ref": "fill-duplicate",
            "occurred_at": self.clock.now,
            "realized_pnl": Decimal("-1"),
        }
        self.assertTrue(self.ledger.record_realized_pnl(**values))
        self.clock.now += timedelta(seconds=1)
        self.assertFalse(self.ledger.record_realized_pnl(**values))

        with self.assertRaises(StateConflict):
            self.ledger.record_realized_pnl(
                **{**values, "realized_pnl": Decimal("-2")}
            )
        with self.assertRaises(StateConflict):
            self.ledger.record_realized_pnl(
                **{**values, "event_id": "different-id"}
            )

        coverage = {
            "coverage_id": "coverage-duplicate",
            "source": "fills",
            "covered_from": self.clock.now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
            "covered_through": self.clock.now,
            "source_cursor_hash": digest("coverage-duplicate"),
        }
        self.assertTrue(self.ledger.record_coverage(**coverage))
        self.assertFalse(self.ledger.record_coverage(**coverage))
        with self.assertRaises(StateConflict):
            self.ledger.record_coverage(
                **{**coverage, "covered_from": self.clock.now - timedelta(hours=1)}
            )
        with self.assertRaises(StateConflict):
            self.ledger.record_coverage(
                **{**coverage, "coverage_id": "different-coverage"}
            )

    def test_utc_rollover_resets_usage_but_requires_fresh_day_coverage(self) -> None:
        self.clock.now = datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc)
        self.cover_current_day()
        self.ledger.record_realized_pnl(
            event_id="day-one-loss",
            source_ref="day-one-fill",
            occurred_at=self.clock.now,
            realized_pnl=Decimal("-9"),
        )
        self.assertEqual(self.ledger.snapshot().used, Decimal("9"))

        self.clock.now = datetime(2026, 8, 26, 0, 1, tzinfo=timezone.utc)
        rolled = self.ledger.snapshot(require_complete=False)
        self.assertEqual(rolled.utc_day.isoformat(), "2026-08-26")
        self.assertEqual(rolled.used, Decimal("0"))
        self.assertFalse(rolled.coverage_complete)
        with self.assertRaises(IncompleteDailyLossCoverage):
            self.ledger.snapshot()

        self.cover_current_day()
        ready = self.ledger.snapshot()
        self.assertEqual(ready.used, Decimal("0"))
        self.assertEqual(ready.remaining, Decimal("25"))

    def test_reopen_is_durable_and_configuration_drift_fails(self) -> None:
        self.cover_current_day()
        self.ledger.record_fee(
            event_id="persisted-fee",
            source_ref="persisted-fill",
            occurred_at=self.clock.now,
            fee=Decimal("1.01"),
        )
        reopened = DailyLossLedger(
            self.database.absolute(), binding=binding(), clock=self.clock
        )
        self.assertEqual(reopened.snapshot().used, Decimal("1.01"))

        drifted = (
            binding(config_hash=digest("config-v2")),
            binding(daily_loss_limit=Decimal("24")),
            binding(account_id="other-testnet-account"),
        )
        for changed in drifted:
            with self.subTest(changed=changed):
                with self.assertRaises(ExecutorConfigDrift):
                    DailyLossLedger(
                        self.database.absolute(), binding=changed, clock=self.clock
                    )

    def test_float_negative_fee_future_event_and_future_coverage_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.ledger.record_realized_pnl(
                event_id="float",
                source_ref="float",
                occurred_at=self.clock.now,
                realized_pnl=-1.0,
            )
        with self.assertRaises(ValidationError):
            self.ledger.record_fee(
                event_id="negative-fee",
                source_ref="negative-fee",
                occurred_at=self.clock.now,
                fee=Decimal("-1"),
            )
        with self.assertRaises(ValidationError):
            self.ledger.record_funding(
                event_id="future",
                source_ref="future",
                occurred_at=self.clock.now + timedelta(seconds=1),
                net_funding=Decimal("-1"),
            )
        with self.assertRaises(ValidationError):
            self.ledger.record_coverage(
                coverage_id="future-coverage",
                source="fills",
                covered_from=self.clock.now,
                covered_through=self.clock.now + timedelta(seconds=1),
                source_cursor_hash=digest("future"),
            )

    def test_tampered_event_fails_closed(self) -> None:
        self.cover_current_day()
        self.ledger.record_fee(
            event_id="tamper-fee",
            source_ref="tamper-fill",
            occurred_at=self.clock.now,
            fee=Decimal("1"),
        )
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TRIGGER daily_loss_events_no_update")
            connection.execute(
                "UPDATE daily_loss_events SET debit = '0' WHERE event_id = 'tamper-fee'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.ledger.snapshot()


if __name__ == "__main__":
    unittest.main()
