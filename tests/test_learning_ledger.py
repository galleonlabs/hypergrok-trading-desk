from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from trading_harness.learning_ledger import (
    ApprovalReference,
    ApprovalState,
    CloseObservation,
    ClosePnlCorrection,
    ComponentVersions,
    CounterfactualEntryRule,
    CounterfactualSpec,
    DecisionClass,
    DecisionCycle,
    DuplicateConflict,
    ExecutionReference,
    ExecutionState,
    ExitReason,
    FillRole,
    FundingPayment,
    IdempotencyConflict,
    LedgerBackdatingError,
    LedgerIntegrityError,
    LearningLedger,
    LearningLedgerError,
    LifecycleError,
    MarketBar,
    MarketPathEvidence,
    ProposedBracket,
    SourceEvidence,
    VenueFill,
)
from trading_harness.post_trade_review import (
    INTERPRETATION_BOUNDARY,
    PostTradeReviewer,
)
from trading_harness.staging_inbox import TradeStagingInbox, TrustedQuoteDecision


START = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(microseconds=1)
        return current


def versions(configuration: str = "config-v1") -> ComponentVersions:
    return ComponentVersions(
        strategy_version="learning-policy-v1",
        configuration_version=configuration,
        code_hash="a" * 64,
        decision_rule_version="decision-v2",
        ta_version="ta-v3",
        sentiment_version="sentiment-manual-v1",
        risk_policy_version="risk-v4",
    )


def source(index: int = 0) -> SourceEvidence:
    return SourceEvidence(
        source_id=f"market-{index}",
        source_kind="completed_candles",
        content_hash=f"{index + 1:064x}",
        observed_at=START - timedelta(minutes=5),
        captured_at=START - timedelta(minutes=1),
        source_version="hyperliquid-candles-v1",
        locator="hyperliquid:ETH:4h",
    )


def decision(
    cycle_id: str = "cycle-1",
    classification: DecisionClass = DecisionClass.BUY,
    *,
    configuration: str = "config-v1",
) -> DecisionCycle:
    bracket = None
    if classification in (DecisionClass.BUY, DecisionClass.SELL):
        if classification is DecisionClass.BUY:
            entry, stop, target = "100", "95", "110"
        else:
            entry, stop, target = "100", "105", "90"
        bracket = ProposedBracket(
            side=classification,
            entry_price=Decimal(entry),
            stop_price=Decimal(stop),
            target_price=Decimal(target),
            quantity=Decimal("2"),
            risk_amount=Decimal("10"),
            settlement_asset="USDC",
        )
    return DecisionCycle(
        cycle_id=cycle_id,
        asset="ETH",
        instrument="ETH-PERP",
        venue="hyperliquid",
        environment="testnet",
        timeframe="4h",
        decided_at=START,
        classification=classification,
        rationale_code="registered_rule_result",
        thesis_hash="b" * 64,
        versions=versions(configuration),
        evidence=(source(),),
        confidence=(
            None if classification is DecisionClass.UNAVAILABLE else Decimal("0.61")
        ),
        bracket=bracket,
        tags=("scheduled", "learning"),
    )


class LearningLedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "learning.sqlite3"
        self.clock = MutableClock(START + timedelta(minutes=2))
        self.ledger = LearningLedger(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record_full_trade(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:cycle-1")
        self.ledger.record_approval(
            ApprovalReference(
                cycle_id="cycle-1",
                reference_id="approval-1",
                state=ApprovalState.APPROVED,
                occurred_at=START + timedelta(seconds=1),
                ticket_hash="c" * 64,
                authority_kind="trusted_local_terminal",
                authority_evidence_hash="d" * 64,
            ),
            idempotency_key="approval:1",
        )
        self.ledger.record_execution(
            ExecutionReference(
                cycle_id="cycle-1",
                command_id="command-1",
                state=ExecutionState.RECONCILED,
                occurred_at=START + timedelta(seconds=2),
                execution_record_hash="e" * 64,
                client_order_ids=("entry-1", "stop-1", "target-1"),
            ),
            idempotency_key="execution:1",
        )
        self.ledger.record_fill(
            VenueFill(
                cycle_id="cycle-1",
                command_id="command-1",
                fill_id="fill-entry",
                order_id="venue-entry",
                client_order_id="entry-1",
                role=FillRole.ENTRY,
                side=DecisionClass.BUY,
                venue_occurred_at=START + timedelta(seconds=3),
                observed_at=START + timedelta(seconds=3, microseconds=250_000),
                price=Decimal("100"),
                reference_price=Decimal("99"),
                quantity=Decimal("2"),
                fee=Decimal("0.1"),
                fee_asset="USDC",
                venue_evidence_hash="f" * 64,
            ),
            idempotency_key="fill:entry",
        )
        self.ledger.record_funding(
            FundingPayment(
                cycle_id="cycle-1",
                funding_id="funding-1",
                occurred_at=START + timedelta(seconds=4),
                amount=Decimal("-0.05"),
                asset="USDC",
                rate=Decimal("0.0001"),
                position_quantity=Decimal("2"),
                venue_evidence_hash="1" * 64,
            ),
            idempotency_key="funding:1",
        )
        self.ledger.record_fill(
            VenueFill(
                cycle_id="cycle-1",
                command_id="command-1",
                fill_id="fill-exit",
                order_id="venue-exit",
                client_order_id="target-1",
                role=FillRole.PROTECTION,
                side=DecisionClass.SELL,
                venue_occurred_at=START + timedelta(seconds=5),
                observed_at=START + timedelta(seconds=5, microseconds=150_000),
                price=Decimal("110"),
                reference_price=Decimal("109.5"),
                quantity=Decimal("2"),
                fee=Decimal("0.1"),
                fee_asset="USDC",
                venue_evidence_hash="2" * 64,
            ),
            idempotency_key="fill:exit",
        )
        self.ledger.record_market_path(
            MarketPathEvidence(
                cycle_id="cycle-1",
                path_id="path-1",
                source_id="completed-candles-1m",
                source_snapshot_hash="3" * 64,
                source_version="candle-snapshot-v1",
                window_started_at=START + timedelta(seconds=3),
                window_ended_at=START + timedelta(seconds=8),
                captured_at=START + timedelta(seconds=9),
                bars=(
                    MarketBar(
                        opened_at=START + timedelta(seconds=3),
                        closed_at=START + timedelta(seconds=5),
                        open=Decimal("100"),
                        high=Decimal("103"),
                        low=Decimal("98"),
                        close=Decimal("102"),
                    ),
                    MarketBar(
                        opened_at=START + timedelta(seconds=5),
                        closed_at=START + timedelta(seconds=8),
                        open=Decimal("102"),
                        high=Decimal("111"),
                        low=Decimal("101"),
                        close=Decimal("110"),
                    ),
                ),
            ),
            idempotency_key="path:1",
        )
        self.ledger.record_counterfactual(
            "cycle-1",
            "path-1",
            CounterfactualSpec(
                scenario_id="original-bracket",
                side=DecisionClass.BUY,
                entry_price=Decimal("100"),
                stop_price=Decimal("95"),
                target_price=Decimal("110"),
                entry_rule=CounterfactualEntryRule.TOUCH,
                cost_r=Decimal("0.1"),
                max_bars=2,
                scenario_version="v1",
            ),
            idempotency_key="counterfactual:1",
        )
        self.ledger.record_close(
            CloseObservation(
                cycle_id="cycle-1",
                close_id="close-1",
                completed_at=START + timedelta(seconds=8),
                exit_reason=ExitReason.TAKE_PROFIT,
                exit_price=Decimal("110"),
                gross_pnl=Decimal("20"),
                pnl_asset="USDC",
                close_evidence_hash="4" * 64,
            ),
            idempotency_key="close:1",
        )


class DecisionAndImmutabilityTests(LearningLedgerTestCase):
    def test_records_buy_sell_nothing_and_unavailable_with_exact_sources(self) -> None:
        for index, classification in enumerate(DecisionClass):
            cycle = decision(f"cycle-{index}", classification)
            event = self.ledger.record_decision(
                cycle, idempotency_key=f"decision:{index}"
            )
            self.assertEqual(event.payload["classification"], classification.value)
            self.assertEqual(event.payload["evidence"][0]["content_hash"], "0" * 63 + "1")
            if classification in (DecisionClass.NOTHING, DecisionClass.UNAVAILABLE):
                self.assertIsNone(event.payload["bracket"])
        self.assertEqual(len(self.ledger.events()), 4)
        self.assertEqual(len(self.ledger.verify_integrity()), 64)

    def test_stop_and_bracket_are_structural_for_position_decisions(self) -> None:
        with self.assertRaisesRegex(ValueError, "require a proposed bracket"):
            replace(decision(), bracket=None)
        with self.assertRaisesRegex(ValueError, "stop < entry < target"):
            ProposedBracket(
                side=DecisionClass.BUY,
                entry_price=Decimal("100"),
                stop_price=Decimal("101"),
                target_price=Decimal("110"),
                quantity=Decimal("1"),
                risk_amount=Decimal("1"),
                settlement_asset="USDC",
            )

    def test_binary_float_is_rejected_before_persistence(self) -> None:
        with self.assertRaisesRegex(TypeError, "floats are forbidden"):
            ProposedBracket(
                side=DecisionClass.BUY,
                entry_price=100.0,
                stop_price=Decimal("95"),
                target_price=Decimal("110"),
                quantity=Decimal("1"),
                risk_amount=Decimal("5"),
                settlement_asset="USDC",
            )
        self.assertEqual(self.ledger.events(), ())

    def test_derived_decimal_values_ignore_ambient_precision(self) -> None:
        def build() -> tuple[Decimal, Decimal]:
            bracket = ProposedBracket(
                side=DecisionClass.BUY,
                entry_price=Decimal("123456789.123456789"),
                stop_price=Decimal("123450000.000000001"),
                target_price=Decimal("123470000.999999999"),
                quantity=Decimal("0.123456789"),
                risk_amount=Decimal("83.812627010589629492"),
                settlement_asset="USDC",
            )
            fill = VenueFill(
                cycle_id="precision-cycle",
                command_id="precision-command",
                fill_id="precision-fill",
                order_id="precision-order",
                client_order_id="precision-client",
                role=FillRole.ENTRY,
                side=DecisionClass.BUY,
                venue_occurred_at=START,
                observed_at=START,
                price=Decimal("123456789.123456789"),
                reference_price=Decimal("123450000.000000001"),
                quantity=Decimal("0.123456789"),
                fee=Decimal("0.01"),
                fee_asset="USDC",
                venue_evidence_hash="f" * 64,
            )
            return bracket.planned_reward_risk, fill.slippage_bps

        with localcontext() as context:
            context.prec = 6
            low_precision = build()
        with localcontext() as context:
            context.prec = 50
            high_precision = build()
        self.assertEqual(low_precision, high_precision)

    def test_low_ambient_precision_cannot_hide_entry_overfill(self) -> None:
        base = decision()
        assert base.bracket is not None
        bracket = replace(
            base.bracket,
            quantity=Decimal("1"),
            risk_amount=Decimal("5"),
        )
        self.ledger.record_decision(
            replace(base, bracket=bracket), idempotency_key="decision:precision-overfill"
        )
        self.ledger.record_execution(
            ExecutionReference(
                cycle_id="cycle-1",
                command_id="command-precision",
                state=ExecutionState.RECONCILED,
                occurred_at=START + timedelta(seconds=1),
                execution_record_hash="e" * 64,
                client_order_ids=("entry-precision", "stop-precision", "target-precision"),
            ),
            idempotency_key="execution:precision-overfill",
        )

        def entry_fill(fill_id: str, quantity: str) -> VenueFill:
            return VenueFill(
                cycle_id="cycle-1",
                command_id="command-precision",
                fill_id=fill_id,
                order_id=f"order-{fill_id}",
                client_order_id="entry-precision",
                role=FillRole.ENTRY,
                side=DecisionClass.BUY,
                venue_occurred_at=START + timedelta(seconds=2),
                observed_at=START + timedelta(seconds=2),
                price=Decimal("100"),
                reference_price=Decimal("100"),
                quantity=Decimal(quantity),
                fee=Decimal("0"),
                fee_asset="USDC",
                venue_evidence_hash=("a" if fill_id == "fill-a" else "b") * 64,
            )

        self.ledger.record_fill(
            entry_fill("fill-a", "0.9999"), idempotency_key="fill:precision:a"
        )
        with localcontext() as context:
            context.prec = 3
            with self.assertRaisesRegex(LifecycleError, "exceed"):
                self.ledger.record_fill(
                    entry_fill("fill-b", "0.0002"),
                    idempotency_key="fill:precision:b",
                )
        fills = [
            event
            for event in self.ledger.events(cycle_id="cycle-1")
            if event.event_type == "venue_fill"
        ]
        self.assertEqual(len(fills), 1)

    def test_restart_and_exact_replay_are_idempotent(self) -> None:
        cycle = decision()
        first = self.ledger.record_decision(cycle, idempotency_key="decision:1")
        restarted = LearningLedger(self.path, clock=self.clock)
        replay = restarted.record_decision(cycle, idempotency_key="decision:1")
        self.assertEqual(first, replay)
        self.assertEqual(len(restarted.events()), 1)

    def test_concurrent_exact_replay_creates_one_event(self) -> None:
        cycle = decision()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(
                pool.map(
                    lambda _: self.ledger.record_decision(
                        cycle, idempotency_key="decision:concurrent"
                    ),
                    range(16),
                )
            )
        self.assertEqual({item.event_id for item in results}, {results[0].event_id})
        self.assertEqual(len(self.ledger.events()), 1)

    def test_concurrent_conflicting_approval_identity_allows_only_one(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")

        class RacingLedger(LearningLedger):
            def __init__(self, path: Path, barrier: threading.Barrier) -> None:
                self.barrier = barrier
                super().__init__(path, clock=self_clock)

            def _cycle_events(self, cycle_id: str):  # type: ignore[no-untyped-def]
                result = super()._cycle_events(cycle_id)
                self.barrier.wait(timeout=5)
                return result

        self_clock = self.clock
        racing = RacingLedger(self.path, threading.Barrier(2))
        references = (
            ApprovalReference(
                cycle_id="cycle-1",
                reference_id="shared-approval",
                state=ApprovalState.REQUESTED,
                occurred_at=START + timedelta(seconds=1),
                ticket_hash="c" * 64,
                authority_kind="terminal-a",
            ),
            ApprovalReference(
                cycle_id="cycle-1",
                reference_id="shared-approval",
                state=ApprovalState.APPROVED,
                occurred_at=START + timedelta(seconds=2),
                ticket_hash="d" * 64,
                authority_kind="terminal-b",
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(
                    racing.record_approval,
                    reference,
                    idempotency_key=f"approval:race:{index}",
                )
                for index, reference in enumerate(references)
            )
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as error:  # The exact loser is scheduler-dependent.
                outcomes.append(error)
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, LifecycleError) for item in outcomes), 1)
        persisted = [
            event
            for event in self.ledger.events(cycle_id="cycle-1")
            if event.event_type == "approval_reference"
        ]
        self.assertEqual(len(persisted), 1)

    def test_concurrent_conflicting_execution_identity_allows_only_one(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")

        class RacingLedger(LearningLedger):
            def __init__(self, path: Path, barrier: threading.Barrier) -> None:
                self.barrier = barrier
                super().__init__(path, clock=self_clock)

            def _cycle_events(self, cycle_id: str):  # type: ignore[no-untyped-def]
                result = super()._cycle_events(cycle_id)
                self.barrier.wait(timeout=5)
                return result

        self_clock = self.clock
        racing = RacingLedger(self.path, threading.Barrier(2))
        references = (
            ExecutionReference(
                cycle_id="cycle-1",
                command_id="shared-command",
                state=ExecutionState.STAGED,
                occurred_at=START + timedelta(seconds=1),
                execution_record_hash="e" * 64,
                client_order_ids=("entry-a", "stop-a", "target-a"),
            ),
            ExecutionReference(
                cycle_id="cycle-1",
                command_id="shared-command",
                state=ExecutionState.AUTHORIZED,
                occurred_at=START + timedelta(seconds=2),
                execution_record_hash="f" * 64,
                client_order_ids=("entry-b", "stop-b", "target-b"),
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(
                    racing.record_execution,
                    reference,
                    idempotency_key=f"execution:race:{index}",
                )
                for index, reference in enumerate(references)
            )
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as error:
                outcomes.append(error)
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, LifecycleError) for item in outcomes), 1)
        persisted = [
            event
            for event in self.ledger.events(cycle_id="cycle-1")
            if event.event_type == "execution_reference"
        ]
        self.assertEqual(len(persisted), 1)

    def test_idempotency_and_logical_duplicate_conflicts_are_rejected(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        with self.assertRaises(IdempotencyConflict):
            self.ledger.record_decision(
                decision("cycle-2"), idempotency_key="decision:1"
            )
        with self.assertRaises(DuplicateConflict):
            self.ledger.record_decision(decision(), idempotency_key="another-key")

    def test_local_append_clock_cannot_backdate(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        self.clock.value = START + timedelta(minutes=1)
        with self.assertRaises(LedgerBackdatingError):
            self.ledger.record_decision(
                decision("cycle-2", DecisionClass.NOTHING),
                idempotency_key="decision:2",
            )

    def test_historical_decision_backfill_is_rejected(self) -> None:
        self.clock.value = START + timedelta(minutes=6)
        with self.assertRaisesRegex(LedgerBackdatingError, "anti-backdating"):
            self.ledger.record_decision(decision(), idempotency_key="historical")

    def test_in_memory_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "file-backed"):
            LearningLedger(":memory:")

    def test_lifecycle_fact_cannot_predate_its_decision(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        with self.assertRaisesRegex(LifecycleError, "must not precede"):
            self.ledger.record_approval(
                ApprovalReference(
                    cycle_id="cycle-1",
                    reference_id="approval-before-decision",
                    state=ApprovalState.REQUESTED,
                    occurred_at=START - timedelta(microseconds=1),
                    ticket_hash="c" * 64,
                    authority_kind="trusted_local_terminal",
                ),
                idempotency_key="approval:backdated",
            )

    def test_sql_updates_and_deletes_are_rejected(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
                connection.execute(
                    "UPDATE learning_ledger_events SET cycle_id = 'forged' WHERE sequence = 1"
                )
            connection.rollback()
            with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
                connection.execute("DELETE FROM learning_ledger_events WHERE sequence = 1")
        finally:
            connection.close()
        self.assertEqual(self.ledger.events()[0].cycle_id, "cycle-1")

    def test_out_of_band_tamper_is_detected_on_restart(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER learning_ledger_no_update")
            connection.execute(
                "UPDATE learning_ledger_events SET payload_json = '{}' WHERE sequence = 1"
            )
            connection.execute(
                """
                CREATE TRIGGER learning_ledger_no_update
                BEFORE UPDATE ON learning_ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'learning ledger events are immutable');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()
        before = self.path.read_bytes()
        with self.assertRaises(LedgerIntegrityError):
            LearningLedger(self.path, clock=self.clock, must_exist=True)
        self.assertEqual(before, self.path.read_bytes())


class ExistingOnlyVerificationTests(LearningLedgerTestCase):
    def test_learning_event_payload_is_bounded_before_transaction(self) -> None:
        before = self.path.read_bytes()

        with self.assertRaisesRegex(LearningLedgerError, "payload exceeds"):
            self.ledger._append(
                event_type="oversized_test_event",
                cycle_id="oversized-cycle",
                semantic_key="oversized-semantic",
                idempotency_key="oversized-idempotency",
                occurred_at=START,
                payload={"evidence": "x" * (1024 * 1024)},
            )

        self.assertEqual(before, self.path.read_bytes())

    def test_live_append_stops_before_crossing_shared_state_limit(self) -> None:
        blocked = False
        limit = 192 * 1024
        with (
            patch("trading_harness.learning_ledger.MAX_SHARED_STATE_FILE_BYTES", limit),
            patch(
                "trading_harness.learning_ledger._LEARNING_WRITE_HEADROOM_BYTES",
                32 * 1024,
            ),
        ):
            for index in range(64):
                try:
                    self.ledger._append(
                        event_type="bounded_test_event",
                        cycle_id=f"bounded-cycle-{index}",
                        semantic_key=f"bounded-semantic-{index}",
                        idempotency_key=f"bounded-idempotency-{index}",
                        occurred_at=START,
                        payload={"evidence": "x" * 8192},
                    )
                except LedgerIntegrityError:
                    blocked = True
                    break

        self.assertTrue(blocked)
        for path in (self.path, Path(f"{self.path}-wal")):
            if path.exists():
                self.assertLessEqual(path.stat().st_size, limit)

    def test_existing_only_rejects_invalid_stores_without_mutating_main_file(self) -> None:
        zero_byte = Path(self.temporary.name) / "zero-byte.sqlite3"
        zero_byte.touch()

        schema_less = Path(self.temporary.name) / "schema-less.sqlite3"
        connection = sqlite3.connect(schema_less)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()

        wrong_store = Path(self.temporary.name) / "staging-store.sqlite3"
        TradeStagingInbox(
            wrong_store,
            quote_callback=lambda request: TrustedQuoteDecision.blocked(
                block_code="nothing_to_trade"
            ),
        )

        for path in (zero_byte, schema_less, wrong_store):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                before_modified = path.stat().st_mtime_ns
                with self.assertRaises(LedgerIntegrityError):
                    LearningLedger(path, clock=self.clock, must_exist=True)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(before_modified, path.stat().st_mtime_ns)

    def test_existing_only_does_not_repair_schema_or_metadata(self) -> None:
        cases = (
            (
                "metadata",
                "DELETE FROM learning_ledger_meta WHERE key = 'schema_version'",
                "SELECT count(*) FROM learning_ledger_meta",
            ),
            (
                "index",
                "DROP INDEX learning_ledger_cycle_sequence",
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'learning_ledger_cycle_sequence'",
            ),
            (
                "trigger",
                "DROP TRIGGER learning_ledger_no_delete",
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'learning_ledger_no_delete'",
            ),
        )
        for name, mutation, absent_query in cases:
            with self.subTest(name=name):
                path = Path(self.temporary.name) / f"missing-{name}.sqlite3"
                LearningLedger(path, clock=self.clock)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(mutation)
                    connection.commit()
                finally:
                    connection.close()
                before = path.read_bytes()

                with self.assertRaises(LedgerIntegrityError):
                    LearningLedger(path, clock=self.clock, must_exist=True)

                self.assertEqual(before, path.read_bytes())
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    self.assertEqual(0, connection.execute(absent_query).fetchone()[0])
                finally:
                    connection.close()

    def test_existing_only_valid_reopen_keeps_later_operations_writable(self) -> None:
        reopened = LearningLedger(self.path, clock=self.clock, must_exist=True)
        event = reopened.record_decision(
            decision(), idempotency_key="existing-only:decision"
        )
        self.assertEqual(event, reopened.events()[0])

    def test_existing_only_verification_includes_a_retained_wal(self) -> None:
        path = Path(self.temporary.name) / "retained-wal.sqlite3"
        keeper = sqlite3.connect(path)
        try:
            self.assertEqual(
                "wal", keeper.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            )
            keeper.execute("PRAGMA wal_autocheckpoint = 0")
            keeper.execute("BEGIN")
            keeper.execute("SELECT count(*) FROM sqlite_master").fetchone()
            ledger = LearningLedger(path, clock=self.clock)
            expected = ledger.record_decision(
                decision(), idempotency_key="retained-wal:decision"
            )
            wal_path = Path(f"{path}-wal")
            self.assertTrue(wal_path.is_file())
            self.assertGreater(wal_path.stat().st_size, 0)
            main_before = path.read_bytes()
            wal_before = wal_path.read_bytes()

            reopened = LearningLedger(path, clock=self.clock, must_exist=True)

            self.assertEqual(main_before, path.read_bytes())
            self.assertEqual(wal_before, wal_path.read_bytes())
            self.assertEqual((expected,), reopened.events())
        finally:
            keeper.close()

    def test_existing_only_rejects_sidecar_symlinks_without_touching_target(self) -> None:
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = Path(self.temporary.name) / f"symlink{suffix}.sqlite3"
                LearningLedger(path, clock=self.clock)
                main_before = path.read_bytes()
                target = Path(self.temporary.name) / f"target{suffix}"
                target.write_bytes(b"do-not-touch")
                sidecar = Path(f"{path}{suffix}")
                sidecar.unlink(missing_ok=True)
                sidecar.symlink_to(target)

                with self.assertRaisesRegex(LedgerIntegrityError, "regular file"):
                    LearningLedger(path, clock=self.clock, must_exist=True)

                self.assertEqual(b"do-not-touch", target.read_bytes())
                self.assertEqual(main_before, path.read_bytes())


class OutcomeAndReviewTests(LearningLedgerTestCase):
    def test_aggregate_uses_one_immutable_event_snapshot(self) -> None:
        first = self.ledger.record_decision(
            decision("cycle-first", DecisionClass.NOTHING),
            idempotency_key="decision:first",
        )
        original_events = self.ledger.events
        calls = 0

        def events_with_concurrent_append(*, cycle_id: str | None = None):
            nonlocal calls
            calls += 1
            snapshot = original_events(cycle_id=cycle_id)
            if calls == 1 and cycle_id is None:
                self.ledger.record_decision(
                    decision("cycle-second", DecisionClass.NOTHING),
                    idempotency_key="decision:second",
                )
            return snapshot

        self.ledger.events = events_with_concurrent_append  # type: ignore[method-assign]
        reports = PostTradeReviewer(self.ledger).aggregate_by_version()
        self.assertEqual(calls, 1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].decision_cycle_count, 1)
        self.assertEqual(reports[0].as_of_event_hash, first.event_hash)
        self.assertEqual(len(original_events()), 2)

    def test_full_lifecycle_derives_costs_excursion_r_and_counterfactual(self) -> None:
        self.record_full_trade()
        events = self.ledger.events(cycle_id="cycle-1")
        self.assertEqual([event.sequence for event in events], list(range(1, 10)))
        for left, right in zip(events, events[1:]):
            self.assertEqual(right.previous_hash, left.event_hash)

        path = next(item for item in events if item.event_type == "market_path")
        self.assertEqual(path.payload["actual_excursion"]["mae_r"], "0.4")
        self.assertEqual(path.payload["actual_excursion"]["mfe_r"], "2.2")
        counterfactual = next(
            item for item in events if item.event_type == "counterfactual"
        )
        self.assertEqual(counterfactual.payload["outcome"], "target")
        self.assertEqual(counterfactual.payload["gross_r"], "2")
        self.assertEqual(counterfactual.payload["net_r_after_assumed_cost"], "1.9")
        self.assertEqual(len(counterfactual.payload["path_hash"]), 64)
        close = next(item for item in events if item.event_type == "close_outcome")
        self.assertEqual(close.payload["total_fees"], "0.2")
        self.assertEqual(close.payload["total_funding"], "-0.05")
        self.assertEqual(close.payload["net_pnl"], "19.75")
        self.assertEqual(close.payload["realized_r"], "1.975")
        self.assertEqual(close.payload["excursion_path_event_hash"], path.event_hash)

        review = PostTradeReviewer(self.ledger).review_cycle("cycle-1")
        self.assertEqual(review.planned_reward_risk, Decimal("2"))
        self.assertEqual(review.mae_r, Decimal("0.4"))
        self.assertEqual(review.mfe_r, Decimal("2.2"))
        self.assertEqual(review.realized_r, Decimal("1.975"))
        self.assertEqual(review.realized_r_risk_basis_amount, Decimal("10"))
        self.assertEqual(review.decision_to_first_entry_us, 3_000_000)
        self.assertEqual(review.holding_duration_us, 5_000_000)
        self.assertEqual(review.exit_reason, "take_profit")
        self.assertEqual(review.counterfactuals[0].net_r_after_assumed_cost, Decimal("1.9"))
        self.assertEqual(review.data_quality_flags, ())
        self.assertEqual(review.interpretation_boundary, INTERPRETATION_BOUNDARY)
        self.assertEqual(len(review.review_hash), 64)

    def test_counterfactual_is_stop_first_when_ohlc_touches_both(self) -> None:
        self.ledger.record_decision(
            decision("cycle-skip", DecisionClass.NOTHING),
            idempotency_key="decision:skip",
        )
        self.ledger.record_market_path(
            MarketPathEvidence(
                cycle_id="cycle-skip",
                path_id="volatile-bar",
                source_id="market",
                source_snapshot_hash="5" * 64,
                source_version="v1",
                window_started_at=START + timedelta(seconds=1),
                window_ended_at=START + timedelta(seconds=2),
                captured_at=START + timedelta(minutes=1),
                bars=(
                    MarketBar(
                        opened_at=START + timedelta(seconds=1),
                        closed_at=START + timedelta(seconds=2),
                        open=Decimal("100"),
                        high=Decimal("111"),
                        low=Decimal("94"),
                        close=Decimal("101"),
                    ),
                ),
            ),
            idempotency_key="path:volatile",
        )
        event = self.ledger.record_counterfactual(
            "cycle-skip",
            "volatile-bar",
            CounterfactualSpec(
                scenario_id="hypothesis-a",
                side=DecisionClass.BUY,
                entry_price=Decimal("100"),
                stop_price=Decimal("95"),
                target_price=Decimal("110"),
                entry_rule=CounterfactualEntryRule.TOUCH,
                cost_r=Decimal("0.05"),
                max_bars=1,
                scenario_version="frozen-v1",
            ),
            idempotency_key="counterfactual:volatile",
        )
        self.assertEqual(event.payload["outcome"], "stop")
        self.assertTrue(event.payload["same_bar_conservative_stop"])
        self.assertEqual(event.payload["net_r_after_assumed_cost"], "-1.05")

    def test_counterfactual_requires_entry_and_handles_entry_bar_ambiguity(self) -> None:
        for cycle_id, path_id, bar, scenario_id in (
            (
                "cycle-never-entered",
                "path-never-entered",
                MarketBar(
                    opened_at=START + timedelta(seconds=1),
                    closed_at=START + timedelta(seconds=2),
                    open=Decimal("111"),
                    high=Decimal("112"),
                    low=Decimal("105"),
                    close=Decimal("111"),
                ),
                "never-entered",
            ),
            (
                "cycle-ambiguous-entry",
                "path-ambiguous-entry",
                MarketBar(
                    opened_at=START + timedelta(seconds=1),
                    closed_at=START + timedelta(seconds=2),
                    open=Decimal("105"),
                    high=Decimal("111"),
                    low=Decimal("99"),
                    close=Decimal("105"),
                ),
                "ambiguous-entry",
            ),
        ):
            self.ledger.record_decision(
                decision(cycle_id, DecisionClass.NOTHING),
                idempotency_key=f"decision:{cycle_id}",
            )
            self.ledger.record_market_path(
                MarketPathEvidence(
                    cycle_id=cycle_id,
                    path_id=path_id,
                    source_id=path_id,
                    source_snapshot_hash=("a" if "never" in path_id else "b") * 64,
                    source_version="v1",
                    window_started_at=bar.opened_at,
                    window_ended_at=bar.closed_at,
                    captured_at=START + timedelta(seconds=3),
                    bars=(bar,),
                ),
                idempotency_key=f"path:{path_id}",
            )
            result = self.ledger.record_counterfactual(
                cycle_id,
                path_id,
                CounterfactualSpec(
                    scenario_id=scenario_id,
                    side=DecisionClass.BUY,
                    entry_price=Decimal("100"),
                    stop_price=Decimal("95"),
                    target_price=Decimal("110"),
                    entry_rule=CounterfactualEntryRule.TOUCH,
                    cost_r=Decimal("0.1"),
                    max_bars=1,
                    scenario_version="v1",
                ),
                idempotency_key=f"counterfactual:{scenario_id}",
            )
            if scenario_id == "never-entered":
                self.assertFalse(result.payload["entered"])
                self.assertEqual(result.payload["outcome"], "not_entered")
                self.assertEqual(result.payload["gross_r"], "0")
                self.assertEqual(result.payload["assumed_cost_r_applied"], "0")
                self.assertEqual(result.payload["net_r_after_assumed_cost"], "0")
                self.assertIsNone(result.payload["entry_bar_opened_at"])
            else:
                self.assertTrue(result.payload["entered"])
                self.assertTrue(result.payload["same_bar_entry_ambiguity"])
                self.assertTrue(result.payload["same_bar_target_ignored"])
                self.assertEqual(result.payload["outcome"], "time_mark")
                self.assertEqual(result.payload["net_r_after_assumed_cost"], "0.9")
                self.assertEqual(result.payload["entry_time_precision"], "bar_interval")
                self.assertEqual(result.payload["exit_time_precision"], "exact_bar_close")
            self.assertNotIn("entered_at", result.payload)
            self.assertNotIn("exit_at", result.payload)

    def test_counterfactual_scenario_version_is_not_double_counted_across_paths(self) -> None:
        self.ledger.record_decision(
            decision("cycle-counterfactual", DecisionClass.NOTHING),
            idempotency_key="decision:counterfactual",
        )
        for index in (1, 2):
            self.ledger.record_market_path(
                MarketPathEvidence(
                    cycle_id="cycle-counterfactual",
                    path_id=f"scenario-path-{index}",
                    source_id=f"source-{index}",
                    source_snapshot_hash=f"{index:064x}",
                    source_version="v1",
                    window_started_at=START + timedelta(seconds=1),
                    window_ended_at=START + timedelta(seconds=2),
                    captured_at=START + timedelta(seconds=3 + index),
                    bars=(
                        MarketBar(
                            opened_at=START + timedelta(seconds=1),
                            closed_at=START + timedelta(seconds=2),
                            open=Decimal("100"),
                            high=Decimal("111"),
                            low=Decimal("99"),
                            close=Decimal("110"),
                        ),
                    ),
                ),
                idempotency_key=f"path:scenario:{index}",
            )
        spec = CounterfactualSpec(
            scenario_id="one-observation-per-cycle",
            side=DecisionClass.BUY,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            target_price=Decimal("110"),
            entry_rule=CounterfactualEntryRule.TOUCH,
            cost_r=Decimal("0.1"),
            max_bars=1,
            scenario_version="v1",
        )
        self.ledger.record_counterfactual(
            "cycle-counterfactual",
            "scenario-path-1",
            spec,
            idempotency_key="counterfactual:scenario:1",
        )
        with self.assertRaises(DuplicateConflict):
            self.ledger.record_counterfactual(
                "cycle-counterfactual",
                "scenario-path-2",
                spec,
                idempotency_key="counterfactual:scenario:2",
            )
        report = PostTradeReviewer(self.ledger).aggregate_by_version()[0]
        self.assertEqual(report.counterfactuals[0].observation_count, 1)
        self.assertEqual(report.counterfactuals[0].entry_rule, "touch")

    def test_versioned_aggregate_includes_abstentions_and_only_descriptive_metrics(self) -> None:
        self.record_full_trade()
        self.ledger.record_decision(
            decision("cycle-nothing", DecisionClass.NOTHING),
            idempotency_key="decision:nothing",
        )
        self.ledger.record_decision(
            decision(
                "cycle-unavailable",
                DecisionClass.UNAVAILABLE,
                configuration="config-v2",
            ),
            idempotency_key="decision:unavailable",
        )
        reports = PostTradeReviewer(self.ledger).aggregate_by_version()
        self.assertEqual(len(reports), 2)
        v1 = next(
            report
            for report in reports
            if report.versions.configuration_version == "config-v1"
        )
        self.assertEqual(v1.decision_cycle_count, 2)
        self.assertEqual(v1.buy_count, 1)
        self.assertEqual(v1.nothing_count, 1)
        self.assertEqual(v1.closed_cycle_count, 1)
        self.assertEqual(v1.realized_r_observation_count, 1)
        self.assertEqual(v1.total_realized_r, Decimal("1.975"))
        self.assertEqual(v1.mean_realized_r, Decimal("1.975"))
        self.assertEqual(v1.total_fees_by_asset[0].amount, Decimal("0.2"))
        self.assertEqual(v1.total_net_pnl_by_asset[0].amount, Decimal("19.75"))
        self.assertEqual(v1.mean_holding_duration_us, Decimal("5000000"))
        self.assertEqual(v1.counterfactuals[0].observation_count, 1)
        self.assertEqual(v1.interpretation_boundary, INTERPRETATION_BOUNDARY)
        self.assertNotIn("causal", v1.review_algorithm_version)
        self.assertEqual(len(v1.metrics_hash), 64)
        v2 = next(
            report
            for report in reports
            if report.versions.configuration_version == "config-v2"
        )
        self.assertEqual(v2.unavailable_count, 1)
        self.assertEqual(v2.closed_cycle_count, 0)
        self.assertIsNone(v2.mean_realized_r)

    def test_conflicting_exact_excursion_paths_are_reported_as_ambiguous(self) -> None:
        self.record_full_trade()
        self.ledger.record_market_path(
            MarketPathEvidence(
                cycle_id="cycle-1",
                path_id="path-conflict",
                source_id="alternate-market-source",
                source_snapshot_hash="d" * 64,
                source_version="alternate-v1",
                window_started_at=START + timedelta(seconds=3),
                window_ended_at=START + timedelta(seconds=8),
                captured_at=START + timedelta(seconds=12),
                bars=(
                    MarketBar(
                        opened_at=START + timedelta(seconds=3),
                        closed_at=START + timedelta(seconds=5),
                        open=Decimal("100"),
                        high=Decimal("104"),
                        low=Decimal("97"),
                        close=Decimal("102"),
                    ),
                    MarketBar(
                        opened_at=START + timedelta(seconds=5),
                        closed_at=START + timedelta(seconds=8),
                        open=Decimal("102"),
                        high=Decimal("112"),
                        low=Decimal("100"),
                        close=Decimal("110"),
                    ),
                ),
            ),
            idempotency_key="path:conflict",
        )
        review = PostTradeReviewer(self.ledger).review_cycle("cycle-1")
        self.assertIsNone(review.mae_r)
        self.assertIsNone(review.mfe_r)
        self.assertIn("excursion_path_ambiguous", review.data_quality_flags)

    def test_same_venue_fill_cannot_be_rebound_to_another_command(self) -> None:
        self.record_full_trade()
        self.ledger.record_execution(
            ExecutionReference(
                cycle_id="cycle-1",
                command_id="command-2",
                state=ExecutionState.RECONCILED,
                occurred_at=START + timedelta(seconds=7),
                execution_record_hash="a" * 64,
                client_order_ids=("entry-2", "stop-2", "target-2"),
            ),
            idempotency_key="execution:2",
        )
        with self.assertRaises(DuplicateConflict):
            self.ledger.record_fill(
                VenueFill(
                    cycle_id="cycle-1",
                    command_id="command-2",
                    fill_id="fill-entry",
                    order_id="venue-entry",
                    client_order_id="entry-2",
                    role=FillRole.ENTRY,
                    side=DecisionClass.BUY,
                    venue_occurred_at=START + timedelta(seconds=3),
                    observed_at=START + timedelta(seconds=10),
                    price=Decimal("100"),
                    reference_price=Decimal("99"),
                    quantity=Decimal("2"),
                    fee=Decimal("0.1"),
                    fee_asset="USDC",
                    venue_evidence_hash="f" * 64,
                ),
                idempotency_key="fill:rebound",
            )

    def test_close_requires_reconciliation_for_the_fill_owning_command(self) -> None:
        self.ledger.record_decision(decision(), idempotency_key="decision:1")
        for command_id, state, prefix in (
            ("fill-command", ExecutionState.STAGED, "fill"),
            ("unrelated-command", ExecutionState.RECONCILED, "other"),
        ):
            self.ledger.record_execution(
                ExecutionReference(
                    cycle_id="cycle-1",
                    command_id=command_id,
                    state=state,
                    occurred_at=START + timedelta(seconds=1),
                    execution_record_hash=("a" if prefix == "fill" else "b") * 64,
                    client_order_ids=(
                        f"{prefix}-entry",
                        f"{prefix}-stop",
                        f"{prefix}-target",
                    ),
                ),
                idempotency_key=f"execution:{command_id}",
            )
        for role, side, fill_id, client_id, price, occurred in (
            (
                FillRole.ENTRY,
                DecisionClass.BUY,
                "entry-fill",
                "fill-entry",
                "100",
                START + timedelta(seconds=2),
            ),
            (
                FillRole.PROTECTION,
                DecisionClass.SELL,
                "exit-fill",
                "fill-target",
                "110",
                START + timedelta(seconds=4),
            ),
        ):
            self.ledger.record_fill(
                VenueFill(
                    cycle_id="cycle-1",
                    command_id="fill-command",
                    fill_id=fill_id,
                    order_id=f"order-{fill_id}",
                    client_order_id=client_id,
                    role=role,
                    side=side,
                    venue_occurred_at=occurred,
                    observed_at=occurred,
                    price=Decimal(price),
                    reference_price=Decimal(price),
                    quantity=Decimal("2"),
                    fee=Decimal("0.1"),
                    fee_asset="USDC",
                    venue_evidence_hash=("c" if role is FillRole.ENTRY else "d") * 64,
                ),
                idempotency_key=f"fill:{fill_id}",
            )
        self.ledger.record_market_path(
            MarketPathEvidence(
                cycle_id="cycle-1",
                path_id="path-1",
                source_id="market",
                source_snapshot_hash="e" * 64,
                source_version="v1",
                window_started_at=START + timedelta(seconds=2),
                window_ended_at=START + timedelta(seconds=5),
                captured_at=START + timedelta(seconds=6),
                bars=(
                    MarketBar(
                        opened_at=START + timedelta(seconds=2),
                        closed_at=START + timedelta(seconds=5),
                        open=Decimal("100"),
                        high=Decimal("111"),
                        low=Decimal("99"),
                        close=Decimal("110"),
                    ),
                ),
            ),
            idempotency_key="path:1",
        )
        with self.assertRaisesRegex(LifecycleError, "every command owning a fill"):
            self.ledger.record_close(
                CloseObservation(
                    cycle_id="cycle-1",
                    close_id="close-1",
                    completed_at=START + timedelta(seconds=5),
                    exit_reason=ExitReason.TAKE_PROFIT,
                    exit_price=Decimal("110"),
                    gross_pnl=Decimal("20"),
                    pnl_asset="USDC",
                    close_evidence_hash="f" * 64,
                ),
                idempotency_key="close:1",
            )

    def test_same_close_replays_idempotently_but_changed_duplicate_conflicts(self) -> None:
        self.record_full_trade()
        close = CloseObservation(
            cycle_id="cycle-1",
            close_id="close-1",
            completed_at=START + timedelta(seconds=8),
            exit_reason=ExitReason.TAKE_PROFIT,
            exit_price=Decimal("110"),
            gross_pnl=Decimal("20"),
            pnl_asset="USDC",
            close_evidence_hash="4" * 64,
        )
        replay = self.ledger.record_close(close, idempotency_key="close:1")
        self.assertEqual(replay.event_type, "close_outcome")
        with self.assertRaises(DuplicateConflict):
            self.ledger.record_close(
                replace(close, gross_pnl=Decimal("21")),
                idempotency_key="close:changed-key",
            )

    def test_post_close_supplements_rederive_review_without_mutating_close(self) -> None:
        self.record_full_trade()
        reviewer = PostTradeReviewer(self.ledger)
        original = reviewer.review_cycle("cycle-1")
        close_event = next(
            event
            for event in self.ledger.events(cycle_id="cycle-1")
            if event.event_type == "close_outcome"
        )
        self.ledger.record_funding(
            FundingPayment(
                cycle_id="cycle-1",
                funding_id="late-discovered-funding",
                occurred_at=START + timedelta(seconds=4, microseconds=500_000),
                amount=Decimal("-0.01"),
                asset="USDC",
                rate=Decimal("0.00001"),
                position_quantity=Decimal("2"),
                venue_evidence_hash="6" * 64,
            ),
            idempotency_key="funding:supplement",
        )
        for role, side, fill_id, client_id, occurred_at in (
            (
                FillRole.ENTRY,
                DecisionClass.BUY,
                "late-entry",
                "entry-1",
                START + timedelta(seconds=3, microseconds=500_000),
            ),
            (
                FillRole.PROTECTION,
                DecisionClass.SELL,
                "late-exit",
                "target-1",
                START + timedelta(seconds=5, microseconds=500_000),
            ),
        ):
            self.ledger.record_fill(
                VenueFill(
                    cycle_id="cycle-1",
                    command_id="command-1",
                    fill_id=fill_id,
                    order_id=f"venue-{fill_id}",
                    client_order_id=client_id,
                    role=role,
                    side=side,
                    venue_occurred_at=occurred_at,
                    observed_at=START + timedelta(seconds=10),
                    price=Decimal("100") if role is FillRole.ENTRY else Decimal("110"),
                    reference_price=(
                        Decimal("100") if role is FillRole.ENTRY else Decimal("110")
                    ),
                    quantity=Decimal("0.1"),
                    fee=Decimal("0.01"),
                    fee_asset="USDC",
                    venue_evidence_hash=("7" if role is FillRole.ENTRY else "8") * 64,
                ),
                idempotency_key=f"fill:{fill_id}",
            )
        stale_path_review = reviewer.review_cycle("cycle-1")
        self.assertIn("exact_excursion_path_missing", stale_path_review.data_quality_flags)
        self.ledger.record_market_path(
            MarketPathEvidence(
                cycle_id="cycle-1",
                path_id="path-supplement",
                source_id="completed-candles-reconciled",
                source_snapshot_hash="9" * 64,
                source_version="candle-snapshot-v1",
                window_started_at=START + timedelta(seconds=3),
                window_ended_at=START + timedelta(seconds=8),
                captured_at=START + timedelta(seconds=11),
                bars=(
                    MarketBar(
                        opened_at=START + timedelta(seconds=3),
                        closed_at=START + timedelta(seconds=5),
                        open=Decimal("100"),
                        high=Decimal("103"),
                        low=Decimal("98"),
                        close=Decimal("102"),
                    ),
                    MarketBar(
                        opened_at=START + timedelta(seconds=5),
                        closed_at=START + timedelta(seconds=8),
                        open=Decimal("102"),
                        high=Decimal("111"),
                        low=Decimal("101"),
                        close=Decimal("110"),
                    ),
                ),
            ),
            idempotency_key="path:supplement",
        )
        stale_gross_review = reviewer.review_cycle("cycle-1")
        self.assertIsNone(stale_gross_review.net_pnl)
        self.assertIsNone(stale_gross_review.realized_r)
        self.assertIn(
            "close_gross_pnl_basis_stale_or_unknown",
            stale_gross_review.data_quality_flags,
        )
        self.ledger.record_close_pnl_correction(
            ClosePnlCorrection(
                cycle_id="cycle-1",
                correction_id="correction-1",
                observed_at=START + timedelta(seconds=12),
                gross_pnl=Decimal("20"),
                pnl_asset="USDC",
                reason_code="late_fill_reconciliation",
                reconciliation_evidence_hash="c" * 64,
            ),
            idempotency_key="close-correction:1",
        )
        revised = reviewer.review_cycle("cycle-1")
        self.assertEqual(revised.total_funding, Decimal("-0.06"))
        self.assertEqual(revised.total_fees, Decimal("0.22"))
        self.assertEqual(revised.net_pnl, Decimal("19.72"))
        self.assertEqual(revised.realized_r_risk_basis_amount, Decimal("10.5"))
        self.assertEqual(
            revised.realized_r,
            Decimal("1.8780952380952380952380952380952380952380952380952"),
        )
        self.assertEqual(revised.mae_r, Decimal("0.4"))
        self.assertIn("planned_entry_quantity_exceeded", revised.data_quality_flags)
        self.assertNotEqual(revised.review_hash, original.review_hash)
        self.assertEqual(close_event.payload["net_pnl"], "19.75")
        self.assertEqual(
            next(
                event
                for event in self.ledger.events(cycle_id="cycle-1")
                if event.event_type == "close_outcome"
            ),
            close_event,
        )
        replayed_close = self.ledger.record_close(
            CloseObservation(
                cycle_id="cycle-1",
                close_id="close-1",
                completed_at=START + timedelta(seconds=8),
                exit_reason=ExitReason.TAKE_PROFIT,
                exit_price=Decimal("110"),
                gross_pnl=Decimal("20"),
                pnl_asset="USDC",
                close_evidence_hash="4" * 64,
            ),
            idempotency_key="close:1",
        )
        self.assertEqual(replayed_close, close_event)

        with self.assertRaisesRegex(LifecycleError, "occurred by the close"):
            self.ledger.record_funding(
                FundingPayment(
                    cycle_id="cycle-1",
                    funding_id="post-close-funding",
                    occurred_at=START + timedelta(seconds=9),
                    amount=Decimal("-0.01"),
                    asset="USDC",
                    rate=Decimal("0.00001"),
                    position_quantity=Decimal("2"),
                    venue_evidence_hash="6" * 64,
                ),
                idempotency_key="funding:after-close",
            )


if __name__ == "__main__":
    unittest.main()
