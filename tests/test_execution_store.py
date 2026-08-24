from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import trading_harness.execution_store as execution_store_module

from trading_harness.analysis import TechnicalBias, TechnicalSnapshot
from trading_harness.assessment import (
    ProfitabilityGate,
    ProfitabilityStatus,
    build_opportunity_assessment,
)
from trading_harness.domain import Environment
from trading_harness.errors import (
    AdmissionDenied,
    RecordNotFound,
    StateConflict,
    StorageError,
    ValidationError,
)
from trading_harness.execution_store import (
    DispatchPreflight,
    ExecutionStore,
    LegReconciliation,
    SignedEnvelopeEvidence,
    TransportOutcomeEvidence,
    TrustedApproval,
    VenueFill,
)
from trading_harness.hyperliquid_response import parse_order_response
from trading_harness.planning import (
    AccountRiskSnapshot,
    PlanIdentity,
    RiskTicket,
    quote_risk_ticket,
)
from trading_harness.sentiment import (
    CollectionMethod,
    SentimentEvidence,
    SentimentPolicy,
    build_sentiment_snapshot,
)
from trading_harness.store import SQLiteStore


NOW = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_ticket(
    ticket_id: str = "ticket-1",
    *,
    environment: Environment = Environment.TESTNET,
    account_id: str = "testnet-account",
    instrument: str = "ETH-PERP",
    symbol: str = "ETH",
) -> RiskTicket:
    technical = TechnicalSnapshot(
        symbol=symbol,
        interval="4h",
        as_of=NOW,
        candle_close_time=NOW - timedelta(minutes=5),
        config_version="strategy-v1",
        config_hash=digest("config"),
        data_hash=digest("data"),
        completed_candles=1000,
        ignored_incomplete_candles=0,
        close=Decimal("2500"),
        ema_fast=Decimal("2550"),
        ema_slow=Decimal("2500"),
        ema_trend=Decimal("2400"),
        rsi=Decimal("60"),
        atr=Decimal("50"),
        bias=TechnicalBias.BUY,
        stop_price=Decimal("2400"),
        target_price=Decimal("3000"),
        reasons=("fixture",),
    )
    evidence = tuple(
        SentimentEvidence(
            evidence_id=f"e-{index}",
            post_id=f"p-{index}",
            source_url=f"https://x.com/example/status/{index}",
            author_hash=digest(f"a-{index}"),
            content_hash=digest(f"c-{index}"),
            cluster_hash=digest(f"k-{index}"),
            published_at=NOW - timedelta(hours=1),
            observed_at=NOW - timedelta(minutes=1),
            polarity=Decimal("0"),
        )
        for index in range(4)
    )
    sentiment = build_sentiment_snapshot(
        asset_id=instrument,
        query=f"${symbol}",
        query_version="q1",
        classifier_version="classifier-v1",
        method=CollectionMethod.X_API,
        window_start=NOW - timedelta(hours=4),
        window_end=NOW - timedelta(minutes=2),
        collected_at=NOW,
        evidence=evidence,
        excluded_count=0,
        collection_complete=True,
        policy=SentimentPolicy(
            version="p1",
            minimum_posts=4,
            minimum_authors=4,
            trim_fraction=Decimal("0"),
            max_cluster_share=Decimal("0.5"),
            ttl_seconds=900,
        ),
    )
    gate = ProfitabilityGate(
        gate_id="gate-1",
        asset_id=instrument,
        thesis_id="trend-breakout",
        thesis_version="1",
        strategy_version="strategy-v1",
        artifact_hash=digest("validation"),
        status=ProfitabilityStatus.QUALIFIED,
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        oos_trades=120,
        shadow_closed_signals=55,
        net_expectancy_r=Decimal("0.15"),
        lower_confidence_bound_r=Decimal("0.02"),
    )
    assessment = build_opportunity_assessment(
        assessment_id=f"assessment-{ticket_id}",
        asset_id=instrument,
        technical=technical,
        sentiment=sentiment,
        profitability=gate,
        at=NOW,
    )
    account = AccountRiskSnapshot(
        account_id=account_id,
        environment=environment,
        observed_at=NOW - timedelta(seconds=1),
        received_at=NOW,
        equity=Decimal("10000"),
        available_collateral=Decimal("9000"),
        daily_loss_remaining=Decimal("100"),
        open_risk_remaining=Decimal("100"),
        max_notional=Decimal("1000"),
        lot_size=Decimal("0.001"),
        leverage=Decimal("2"),
        artifact_hash=digest(f"account-{environment.value}-{account_id}"),
    )
    identity = PlanIdentity(
        thesis_id="trend-breakout",
        thesis_version="1",
        strategy_version="strategy-v1",
        venue="hyperliquid",
        account_id=account_id,
        environment=environment,
        instrument=instrument,
    )
    return quote_risk_ticket(
        ticket_id=ticket_id,
        assessment=assessment,
        technical=technical,
        identity=identity,
        account=account,
        at=NOW,
    )


def make_approval(
    ticket: RiskTicket,
    approval_id: str = "approval-1",
    *,
    token_text: str = "opaque-token-1",
    environment: Environment = Environment.TESTNET,
    account_id: str = "testnet-account",
    issued_at: datetime = NOW + timedelta(milliseconds=2),
    expires_at: datetime | None = None,
) -> TrustedApproval:
    return TrustedApproval(
        approval_id=approval_id,
        ticket_hash=ticket.ticket_hash,
        token_hash=digest(token_text),
        approver_id="human:alice",
        audience="local-execution-worker",
        environment=environment,
        account_id=account_id,
        issued_at=issued_at,
        expires_at=ticket.expires_at if expires_at is None else expires_at,
    )


class ExecutionStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "execution.sqlite"
        self.store = ExecutionStore(
            self.path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register_approve(
        self,
        ticket_id: str = "ticket-1",
        approval_id: str = "approval-1",
    ) -> tuple[RiskTicket, TrustedApproval]:
        ticket = make_ticket(ticket_id)
        self.store.register_ticket(
            ticket, stored_at=NOW + timedelta(milliseconds=1)
        )
        approval = make_approval(ticket, approval_id)
        self.store.register_approval(approval)
        return ticket, approval

    def admit_one(
        self,
        command_id: str = "command-1",
    ) -> tuple[RiskTicket, TrustedApproval]:
        ticket, approval = self.register_approve()
        self.store.admit(
            command_id=command_id,
            approval_id=approval.approval_id,
            token_hash=approval.token_hash,
            audience=approval.audience,
            at=NOW + timedelta(milliseconds=3),
        )
        return ticket, approval

    def prepare_unknown(
        self, command_id: str = "command-1"
    ) -> tuple[RiskTicket, int]:
        ticket, _ = self.admit_one(command_id)
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        preflight = self.register_preflight(ticket, command_id)
        signed = self.make_signed_evidence(
            preflight, command_id=command_id
        )
        self.store.prepare_attempt(
            command_id,
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=1_777_777_777_777,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        self.store.mark_submitted_unknown(
            command_id,
            "dispatcher",
            claim.fencing_token,
            transport_evidence=self.make_transport_evidence(
                "attempt-1",
                signed,
                command_id=command_id,
                outcome="unknown",
            ),
            at=NOW + timedelta(seconds=3),
        )
        reconcile_claim = self.store.claim_reconciliation(
            command_id,
            "reconciler",
            at=NOW + timedelta(seconds=4),
            lease_seconds=30,
        )
        return ticket, reconcile_claim.fencing_token

    def make_preflight(
        self,
        ticket: RiskTicket,
        command_id: str = "command-1",
        *,
        observed_at: datetime = NOW + timedelta(seconds=1),
        expires_at: datetime = NOW + timedelta(seconds=20),
        passed: bool = True,
        account_snapshot_hash: str | None = None,
    ) -> DispatchPreflight:
        assert ticket.plan is not None
        return DispatchPreflight(
            command_id=command_id,
            ticket_hash=ticket.ticket_hash,
            plan_hash=ticket.plan.plan_hash,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            account_snapshot_hash=(
                digest("send-time-account")
                if account_snapshot_hash is None
                else account_snapshot_hash
            ),
            metadata_hash=digest("metadata"),
            market_snapshot_hash=digest("market"),
            risk_policy_hash=ticket.policy_hash,
            observed_at=observed_at,
            expires_at=expires_at,
            passed=passed,
        )

    def register_preflight(
        self,
        ticket: RiskTicket,
        command_id: str = "command-1",
    ) -> DispatchPreflight:
        preflight = self.make_preflight(ticket, command_id)
        return self.store.register_preflight(
            preflight, at=NOW + timedelta(seconds=1, milliseconds=1)
        )

    def make_signed_evidence(
        self,
        preflight: DispatchPreflight,
        *,
        command_id: str = "command-1",
        nonce: int = 1_777_777_777_777,
        action_hash: str | None = None,
        wire_hash: str | None = None,
    ) -> SignedEnvelopeEvidence:
        return SignedEnvelopeEvidence(
            command_id=command_id,
            preflight_hash=preflight.preflight_hash,
            environment=Environment.TESTNET,
            endpoint="https://api.hyperliquid-testnet.xyz/exchange",
            account_id="testnet-account",
            plan_hash=preflight.plan_hash,
            action_hash=digest("action") if action_hash is None else action_hash,
            nonce=nonce,
            wire_hash=digest("wire") if wire_hash is None else wire_hash,
            signature_hash=digest("signature"),
            envelope_hash=digest("envelope"),
            signer_binding_hash=digest("signer-binding"),
            authorization_expires_at_ms=int(
                preflight.expires_at.timestamp() * 1_000
            ),
            expires_after_ms=int(preflight.expires_at.timestamp() * 1_000),
            signed_at_ms=int((NOW + timedelta(seconds=1)).timestamp() * 1_000),
        )

    def make_transport_evidence(
        self,
        attempt_id: str,
        signed: SignedEnvelopeEvidence,
        *,
        command_id: str = "command-1",
        outcome: str,
        response_hash: str | None = None,
        detail_code: str = "fixture",
    ) -> TransportOutcomeEvidence:
        return TransportOutcomeEvidence(
            command_id=command_id,
            attempt_id=attempt_id,
            signed_evidence_hash=signed.evidence_hash,
            endpoint=signed.endpoint,
            attempted_at_ms=int((NOW + timedelta(seconds=2)).timestamp() * 1_000),
            outcome=outcome,
            http_status=200 if outcome == "response_received" else None,
            detail_code=detail_code,
            response_hash=response_hash,
            transport_attempt_hash=digest(f"transport-{attempt_id}-{outcome}"),
            send_count=1,
            retry_performed=False,
            venue_write_attempted=True,
        )


class MigrationAndIdentityTests(ExecutionStoreTestCase):
    def test_schema_is_checksummed_wal_and_can_coexist(self) -> None:
        combined = Path(self.temporary.name) / "combined.sqlite"
        SQLiteStore(combined)
        ExecutionStore(
            combined,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        connection = sqlite3.connect(combined)
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            migrations = connection.execute(
                """
                SELECT version, checksum
                FROM execution_schema_migrations ORDER BY version
                """
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual([1, 2, 3], [row[0] for row in migrations])
        self.assertTrue(all(len(row[1]) == 64 for row in migrations))
        self.assertIn("commands", tables)
        self.assertIn("execution_commands", tables)

    def test_environment_account_and_caps_are_immutable(self) -> None:
        for changes in (
            {"account_id": "another-account"},
            {"max_reserved_loss": "101"},
            {"max_reserved_notional": "2001"},
        ):
            values = {
                "environment": Environment.TESTNET,
                "account_id": "testnet-account",
                "max_reserved_loss": "100",
                "max_reserved_notional": "2000",
            }
            values.update(changes)
            with self.assertRaises(StorageError):
                ExecutionStore(self.path, **values)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ExecutionStore(
                self.path,
                environment=Environment.MAINNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )

    def test_migration_or_identity_tamper_fails_closed(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_schema_migrations SET checksum = ?", (digest("bad"),)
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )

    def test_identity_record_tamper_fails_closed(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_store_identity SET account_id = 'tampered'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )

    def test_store_rejects_shadow_mainnet_and_cross_environment_ticket(self) -> None:
        for environment in (Environment.SHADOW, Environment.MAINNET):
            with self.assertRaises(ValidationError):
                ExecutionStore(
                    Path(self.temporary.name) / f"{environment.value}.sqlite",
                    environment=environment,
                    account_id=environment.value,
                    max_reserved_loss="1",
                    max_reserved_notional="1",
                )
        mainnet_ticket = make_ticket(
            "mainnet-ticket",
            environment=Environment.MAINNET,
            account_id="mainnet-account",
        )
        with self.assertRaises(ValidationError):
            self.store.register_ticket(
                mainnet_ticket, stored_at=NOW + timedelta(milliseconds=1)
            )

    def test_v1_database_migrates_forward_to_preflight_schema(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy-v1.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """
                CREATE TABLE execution_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            migration = execution_store_module._SCHEMA_V1
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO execution_schema_migrations (
                    version, name, checksum, applied_at
                ) VALUES (?, ?, ?, ?)
                """,
                (1, migration.name, migration.checksum, NOW.isoformat()),
            )
            connection.commit()
        finally:
            connection.close()
        ExecutionStore(
            legacy_path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        connection = sqlite3.connect(legacy_path)
        try:
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM execution_schema_migrations ORDER BY version"
                )
            ]
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(execution_attempts)")
            }
            preflight_table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'execution_dispatch_preflights'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual([1, 2, 3], versions)
        self.assertIn("preflight_hash", columns)
        self.assertIn("signed_evidence_hash", columns)
        self.assertIn("transport_evidence_hash", columns)
        self.assertIsNotNone(preflight_table)


class TicketApprovalAdmissionTests(ExecutionStoreTestCase):
    def test_exact_plan_ticket_and_opaque_approval_survive_restart(self) -> None:
        ticket, approval = self.register_approve()
        self.assertEqual(
            ticket.as_dict(), self.store.get_ticket_payload(ticket.ticket_hash)
        )
        assert ticket.plan is not None
        self.assertEqual(
            ticket.plan.as_dict(), self.store.get_plan_payload(ticket.plan.plan_hash)
        )
        self.assertEqual("issued", self.store.approval_state(approval.approval_id))
        self.assertEqual(
            ticket.ticket_hash,
            self.store.register_ticket(
                ticket, stored_at=NOW + timedelta(seconds=1)
            ),
        )
        self.assertEqual(approval, self.store.register_approval(approval))
        connection = sqlite3.connect(self.path)
        try:
            persisted_token = connection.execute(
                "SELECT token_hash FROM execution_approvals"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(digest("opaque-token-1"), persisted_token)
        self.assertNotEqual("opaque-token-1", persisted_token)
        restarted = ExecutionStore(
            self.path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        self.assertEqual(ticket.as_dict(), restarted.get_ticket_payload(ticket.ticket_hash))

    def test_admission_consumes_once_reserves_and_creates_three_legs(self) -> None:
        ticket, approval = self.admit_one()
        command = self.store.get_command("command-1")
        outbox = self.store.get_outbox("command-1")
        legs = self.store.get_legs("command-1")
        self.assertEqual("queued", command.state)
        self.assertEqual("queued", outbox.state)
        self.assertEqual(
            ("entry", "protective_stop", "take_profit"),
            tuple(leg.role for leg in legs),
        )
        self.assertEqual(3, len({leg.cloid for leg in legs}))
        self.assertFalse(legs[0].reduce_only)
        self.assertTrue(legs[1].reduce_only)
        self.assertTrue(legs[2].reduce_only)
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])
        self.assertEqual("consumed", self.store.approval_state(approval.approval_id))
        with self.assertRaises(AdmissionDenied):
            self.store.admit(
                command_id="replay",
                approval_id=approval.approval_id,
                token_hash=approval.token_hash,
                audience=approval.audience,
                at=NOW + timedelta(seconds=1),
            )

    def test_wrong_token_audience_and_expiry_leave_no_partial_state(self) -> None:
        ticket, approval = self.register_approve()
        for token, audience, at in (
            (digest("wrong"), approval.audience, NOW + timedelta(seconds=1)),
            (approval.token_hash, "wrong-audience", NOW + timedelta(seconds=1)),
            (approval.token_hash, approval.audience, ticket.expires_at),
        ):
            with self.assertRaises(AdmissionDenied):
                self.store.admit(
                    command_id="never-created",
                    approval_id=approval.approval_id,
                    token_hash=token,
                    audience=audience,
                    at=at,
                )
            self.assertEqual("issued", self.store.approval_state(approval.approval_id))
            self.assertEqual((Decimal("0"), Decimal("0")), self.store.get_reserved_exposure())

    def test_flat_account_gate_rolls_back_second_approval(self) -> None:
        capped_path = Path(self.temporary.name) / "capped.sqlite"
        store = ExecutionStore(
            capped_path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="40",
            max_reserved_notional="1000",
        )
        pairs = []
        for index in (1, 2):
            ticket = make_ticket(
                f"ticket-{index}",
                instrument=("ETH-PERP" if index == 1 else "SOL-PERP"),
                symbol=("ETH" if index == 1 else "SOL"),
            )
            store.register_ticket(ticket, stored_at=NOW + timedelta(milliseconds=1))
            approval = make_approval(ticket, f"approval-{index}", token_text=f"token-{index}")
            store.register_approval(approval)
            pairs.append((ticket, approval))
        store.admit(
            command_id="command-1",
            approval_id="approval-1",
            token_hash=pairs[0][1].token_hash,
            audience=pairs[0][1].audience,
            at=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(AdmissionDenied) as caught:
            store.admit(
                command_id="command-2",
                approval_id="approval-2",
                token_hash=pairs[1][1].token_hash,
                audience=pairs[1][1].audience,
                at=NOW + timedelta(seconds=1),
            )
        self.assertEqual("ACCOUNT_COMMAND_ALREADY_ACTIVE", caught.exception.code)
        self.assertEqual("issued", store.approval_state("approval-2"))
        with self.assertRaises(RecordNotFound):
            store.get_command("command-2")

    def test_revoked_approval_cannot_admit(self) -> None:
        _, approval = self.register_approve()
        self.store.revoke_approval(
            approval.approval_id, at=NOW + timedelta(seconds=1)
        )
        self.assertEqual("revoked", self.store.approval_state(approval.approval_id))
        with self.assertRaises(AdmissionDenied):
            self.store.admit(
                command_id="revoked-command",
                approval_id=approval.approval_id,
                token_hash=approval.token_hash,
                audience=approval.audience,
                at=NOW + timedelta(seconds=2),
            )

    def test_void_unsent_permanently_consumes_authority_and_releases_risk(self) -> None:
        ticket, approval = self.admit_one()
        self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        command = self.store.void_unsent_command(
            "command-1",
            reason="signer failed before attempt persistence",
            at=NOW + timedelta(seconds=2),
        )
        self.assertEqual("terminal", command.state)
        self.assertEqual("terminal", self.store.get_outbox("command-1").state)
        self.assertEqual((Decimal("0"), Decimal("0")), self.store.get_reserved_exposure())
        self.assertEqual("consumed", self.store.approval_state(approval.approval_id))
        self.assertTrue(
            all(leg.status == "expired" for leg in self.store.get_legs("command-1"))
        )
        with self.assertRaises(AdmissionDenied):
            self.store.admit(
                command_id="reuse",
                approval_id=approval.approval_id,
                token_hash=approval.token_hash,
                audience=approval.audience,
                at=NOW + timedelta(seconds=3),
            )
        self.assertEqual(ticket.ticket_hash, command.ticket_hash)

    def test_void_rejects_any_prepared_attempt(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight)
        self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=signed.nonce,
            action_hash=signed.action_hash,
            wire_hash=signed.wire_hash,
            at=NOW + timedelta(seconds=2),
        )
        with self.assertRaises(StateConflict):
            self.store.void_unsent_command(
                "command-1",
                reason="must reconcile",
                at=NOW + timedelta(seconds=3),
            )

    def test_concurrent_approval_consumption_has_one_winner(self) -> None:
        _, approval = self.register_approve()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def consume(command_id: str) -> None:
            store = ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )
            barrier.wait()
            try:
                store.admit(
                    command_id=command_id,
                    approval_id=approval.approval_id,
                    token_hash=approval.token_hash,
                    audience=approval.audience,
                    at=NOW + timedelta(seconds=1),
                )
                result = "success"
            except AdmissionDenied:
                result = "denied"
            with lock:
                outcomes.append(result)

        threads = [
            threading.Thread(target=consume, args=("command-a",)),
            threading.Thread(target=consume, args=("command-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["success", "denied"], outcomes)

    def test_concurrent_flat_account_admission_has_one_owner(self) -> None:
        approvals = []
        for index in (1, 2):
            ticket = make_ticket(f"instrument-ticket-{index}")
            self.store.register_ticket(
                ticket, stored_at=NOW + timedelta(milliseconds=1)
            )
            approval = make_approval(
                ticket,
                f"instrument-approval-{index}",
                token_text=f"instrument-token-{index}",
            )
            self.store.register_approval(approval)
            approvals.append(approval)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []
        lock = threading.Lock()

        def admit(index: int) -> None:
            store = ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )
            approval = approvals[index]
            barrier.wait()
            try:
                store.admit(
                    command_id=f"instrument-command-{index}",
                    approval_id=approval.approval_id,
                    token_hash=approval.token_hash,
                    audience=approval.audience,
                    at=NOW + timedelta(seconds=1),
                )
                outcome = (approval.approval_id, "success")
            except AdmissionDenied as error:
                outcome = (approval.approval_id, error.code)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=admit, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(
            ["success", "ACCOUNT_COMMAND_ALREADY_ACTIVE"],
            [outcome for _, outcome in outcomes],
        )
        states = {
            approval_id: self.store.approval_state(approval_id)
            for approval_id, _ in outcomes
        }
        self.assertCountEqual(["consumed", "issued"], list(states.values()))


class DispatchPreflightTests(ExecutionStoreTestCase):
    def test_preflight_is_exact_fresh_and_bound_into_attempt(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=20
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight, nonce=123)
        self.assertEqual(preflight, self.store.get_preflight("command-1"))
        attempt = self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=123,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        self.assertEqual(preflight.preflight_hash, attempt.preflight_hash)
        self.assertEqual(signed.evidence_hash, attempt.signed_evidence_hash)
        self.assertEqual(signed, self.store.get_signed_evidence("command-1"))
        restarted = ExecutionStore(
            self.path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
        )
        self.assertEqual(
            preflight.preflight_hash,
            restarted.get_attempt("command-1").preflight_hash,
        )

    def test_failed_stale_missing_and_cross_bound_preflight_block_attempt(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=30
        )
        assert claim is not None
        failed = self.make_preflight(ticket, passed=False)
        with self.assertRaises(AdmissionDenied) as failed_error:
            self.store.register_preflight(
                failed, at=NOW + timedelta(seconds=1, milliseconds=1)
            )
        self.assertEqual("DISPATCH_PREFLIGHT_FAILED", failed_error.exception.code)

        stale = self.make_preflight(
            ticket,
            observed_at=NOW - timedelta(seconds=20),
            expires_at=NOW - timedelta(seconds=1),
        )
        with self.assertRaises(AdmissionDenied) as stale_error:
            self.store.register_preflight(
                stale, at=NOW + timedelta(seconds=1, milliseconds=1)
            )
        self.assertEqual("DISPATCH_PREFLIGHT_STALE", stale_error.exception.code)

        wrong_policy = replace(
            self.make_preflight(ticket),
            risk_policy_hash=digest("wrong-policy"),
            preflight_hash="",
        )
        with self.assertRaises(AdmissionDenied) as policy_error:
            self.store.register_preflight(
                wrong_policy,
                at=NOW + timedelta(seconds=1, milliseconds=1),
            )
        self.assertEqual(
            "DISPATCH_PREFLIGHT_POLICY_MISMATCH", policy_error.exception.code
        )

        with self.assertRaises(AdmissionDenied) as missing_error:
            missing_signed = self.make_signed_evidence(
                self.make_preflight(ticket), nonce=123
            )
            self.store.prepare_attempt(
                "command-1",
                "dispatcher",
                claim.fencing_token,
                attempt_id="attempt-missing",
                preflight_hash=digest("missing-preflight"),
                signed_evidence=missing_signed,
                nonce=123,
                action_hash=digest("action"),
                wire_hash=digest("wire"),
                at=NOW + timedelta(seconds=2),
            )
        self.assertEqual("DISPATCH_PREFLIGHT_NOT_FOUND", missing_error.exception.code)

        other_ticket = make_ticket("other-ticket")
        cross_bound = self.make_preflight(other_ticket)
        with self.assertRaises(AdmissionDenied) as binding_error:
            self.store.register_preflight(
                cross_bound, at=NOW + timedelta(seconds=1, milliseconds=1)
            )
        self.assertEqual(
            "DISPATCH_PREFLIGHT_BINDING_MISMATCH", binding_error.exception.code
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_attempt("command-1")

    def test_preflight_cannot_be_swapped_and_staleness_is_rechecked(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=30
        )
        assert claim is not None
        first = self.make_preflight(
            ticket,
            expires_at=NOW + timedelta(seconds=3),
        )
        self.store.register_preflight(
            first, at=NOW + timedelta(seconds=1, milliseconds=1)
        )
        replacement = self.make_preflight(
            ticket,
            account_snapshot_hash=digest("replacement-account"),
        )
        with self.assertRaises(StateConflict):
            self.store.register_preflight(
                replacement, at=NOW + timedelta(seconds=1, milliseconds=2)
            )
        with self.assertRaises(AdmissionDenied) as stale_error:
            stale_signed = self.make_signed_evidence(first, nonce=123)
            self.store.prepare_attempt(
                "command-1",
                "dispatcher",
                claim.fencing_token,
                attempt_id="attempt-stale",
                preflight_hash=first.preflight_hash,
                signed_evidence=stale_signed,
                nonce=123,
                action_hash=digest("action"),
                wire_hash=digest("wire"),
                at=NOW + timedelta(seconds=3),
            )
        self.assertEqual("DISPATCH_PREFLIGHT_STALE", stale_error.exception.code)

    def test_signed_evidence_cannot_outlive_preflight(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=30
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        ordinary = self.make_signed_evidence(preflight, nonce=123)
        later_expiry = int(preflight.expires_at.timestamp() * 1_000) + 1_000
        stale_authority = replace(
            ordinary,
            authorization_expires_at_ms=later_expiry,
            expires_after_ms=later_expiry,
            evidence_hash="",
        )
        with self.assertRaises(AdmissionDenied) as caught:
            self.store.prepare_attempt(
                "command-1",
                "dispatcher",
                claim.fencing_token,
                attempt_id="attempt-outlives-preflight",
                preflight_hash=preflight.preflight_hash,
                signed_evidence=stale_authority,
                nonce=123,
                action_hash=digest("action"),
                wire_hash=digest("wire"),
                at=NOW + timedelta(seconds=2),
            )
        self.assertEqual("SIGNED_EVIDENCE_OUTLIVES_PREFLIGHT", caught.exception.code)

    def test_concurrent_preflight_registration_has_one_binding(self) -> None:
        ticket, _ = self.admit_one()
        self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=30
        )
        candidates = (
            self.make_preflight(ticket),
            self.make_preflight(
                ticket, account_snapshot_hash=digest("other-account-snapshot")
            ),
        )
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def register(candidate: DispatchPreflight) -> None:
            store = ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )
            barrier.wait()
            try:
                store.register_preflight(
                    candidate,
                    at=NOW + timedelta(seconds=1, milliseconds=1),
                )
                outcome = "success"
            except StateConflict:
                outcome = "conflict"
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=register, args=(candidate,))
            for candidate in candidates
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["success", "conflict"], outcomes)

    def test_tampered_preflight_blocks_before_attempt(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=30
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                UPDATE execution_dispatch_preflights SET metadata_hash = ?
                WHERE preflight_hash = ?
                """,
                (digest("tampered-metadata"), preflight.preflight_hash),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            signed = self.make_signed_evidence(preflight, nonce=123)
            self.store.prepare_attempt(
                "command-1",
                "dispatcher",
                claim.fencing_token,
                attempt_id="attempt-tampered",
                preflight_hash=preflight.preflight_hash,
                signed_evidence=signed,
                nonce=123,
                action_hash=digest("action"),
                wire_hash=digest("wire"),
                at=NOW + timedelta(seconds=2),
            )
        with self.assertRaises(RecordNotFound):
            self.store.get_attempt("command-1")


class OutboxCrashAndReplayTests(ExecutionStoreTestCase):
    def test_critical_incident_blocks_entry_dispatch(self) -> None:
        self.admit_one()
        self.store.record_incident(
            incident_id="critical-before-dispatch",
            command_id="command-1",
            code="CRITICAL_FIXTURE",
            severity="critical",
            at=NOW + timedelta(milliseconds=4),
        )
        with self.assertRaises(StateConflict):
            self.store.claim_next(
                "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=5
            )

    def test_atomic_claim_has_one_winner_and_unsent_expiry_requeues(self) -> None:
        self.admit_one()
        barrier = threading.Barrier(2)
        claims = []
        lock = threading.Lock()

        def claim(worker: str) -> None:
            store = ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
            )
            barrier.wait()
            result = store.claim_next(
                worker, at=NOW + timedelta(seconds=1), lease_seconds=5
            )
            with lock:
                claims.append(result)

        threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [value for value in claims if value is not None]
        self.assertEqual(1, len(claimed))
        reclaimed = self.store.claim_next(
            "worker-next", at=NOW + timedelta(seconds=6), lease_seconds=5
        )
        assert reclaimed is not None
        self.assertEqual(2, reclaimed.fencing_token)
        self.assertEqual("worker-next", reclaimed.worker_id)

    def test_prepared_attempt_expiry_becomes_unknown_and_never_retries(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=5
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight)
        attempt = self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=1_777_777_777_777,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        self.assertEqual("prepared", attempt.state)
        self.assertEqual(1_777_777_777_777, attempt.nonce)
        self.assertEqual(digest("wire"), attempt.wire_hash)
        self.assertEqual(preflight.preflight_hash, attempt.preflight_hash)
        self.assertIsNone(
            self.store.claim_next(
                "another-dispatcher",
                at=NOW + timedelta(seconds=6),
                lease_seconds=5,
            )
        )
        self.assertEqual("submitted_unknown", self.store.get_command("command-1").state)
        self.assertEqual("unknown", self.store.get_attempt("command-1").state)
        self.assertEqual(
            "claim_expiry",
            self.store.get_transport_evidence("command-1").evidence_basis,
        )
        self.assertEqual(
            ticket.stressed_loss, self.store.get_reserved_exposure()[0]
        )
        with self.assertRaises(StateConflict):
            self.store.prepare_attempt(
                "command-1",
                "dispatcher",
                claim.fencing_token,
                attempt_id="attempt-2",
                preflight_hash=preflight.preflight_hash,
                signed_evidence=signed,
                nonce=1_777_777_777_778,
                action_hash=digest("action-2"),
                wire_hash=digest("wire-2"),
                at=NOW + timedelta(seconds=7),
            )
        reconciliation = self.store.claim_reconciliation(
            "command-1",
            "reconciler",
            at=NOW + timedelta(seconds=7),
            lease_seconds=10,
        )
        self.assertEqual("reconciling", reconciliation.state)

    def test_explicit_unknown_path_retains_full_reservation(self) -> None:
        ticket, token = self.prepare_unknown()
        self.assertGreater(token, 0)
        self.assertEqual("unknown", self.store.get_attempt("command-1").state)
        self.assertEqual(
            "transport_result",
            self.store.get_transport_evidence("command-1").evidence_basis,
        )
        self.assertTrue(
            all(leg.status == "submitted_unknown" for leg in self.store.get_legs("command-1"))
        )
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])


class ResponseAndReconciliationTests(ExecutionStoreTestCase):
    def _prepared_response_command(self):
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight, nonce=123)
        self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=123,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        return ticket, claim, signed

    def test_three_leg_response_persists_oids_and_requires_reconciliation(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight, nonce=123)
        self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=123,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        response = parse_order_response(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"resting": {"oid": 101}},
                            {"resting": {"oid": 102}},
                            {"resting": {"oid": 103}},
                        ]
                    },
                },
            },
            requested_sizes=(ticket.quantity,) * 3,
        )
        command = self.store.record_submission_response(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            response,
            transport_evidence=self.make_transport_evidence(
                "attempt-1",
                signed,
                outcome="response_received",
                response_hash=digest("transport-response"),
            ),
            at=NOW + timedelta(seconds=3),
        )
        self.assertEqual("reconciling", command.state)
        self.assertEqual(
            [101, 102, 103],
            [leg.venue_oid for leg in self.store.get_legs("command-1")],
        )
        self.assertTrue(
            all(
                leg.status == "resting"
                for leg in self.store.get_legs("command-1")
            )
        )
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])

    def test_only_complete_flat_reconciliation_releases_risk(self) -> None:
        ticket, fencing = self.prepare_unknown()
        legs = self.store.get_legs("command-1")
        half = ticket.quantity / Decimal("2")
        fill = VenueFill(
            fill_id="fill-1",
            role="entry",
            cloid=legs[0].cloid,
            quantity=half,
            price=Decimal("2500"),
            fee=Decimal("0.25"),
            occurred_at=NOW + timedelta(seconds=4),
        )
        partial = self.store.reconcile(
            "command-1",
            "reconciler",
            fencing,
            reconciliation_id="reconciliation-1",
            account_snapshot_hash=digest("snapshot-1"),
            observed_at=NOW + timedelta(seconds=5),
            complete=False,
            legs=(
                LegReconciliation("entry", legs[0].cloid, "partially_filled", half, 201),
                LegReconciliation("protective_stop", legs[1].cloid, "resting", "0", 202),
                LegReconciliation("take_profit", legs[2].cloid, "resting", "0", 203),
            ),
            signed_position_quantity=half,
            protected_quantity="0",
            fills=(fill,),
        )
        self.assertEqual("reconciling", partial.state)
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])
        self.assertEqual(half, self.store.get_position("ETH-PERP").signed_quantity)
        self.assertEqual("under_protected", self.store.get_protection("command-1").state)
        self.assertEqual((fill,), self.store.list_fills("command-1"))
        incidents = self.store.list_incidents("command-1")
        self.assertIn("POSITION_UNDER_PROTECTED", {item.code for item in incidents})

        updated_legs = self.store.get_legs("command-1")
        terminal = self.store.reconcile(
            "command-1",
            "reconciler",
            fencing,
            reconciliation_id="reconciliation-2",
            account_snapshot_hash=digest("snapshot-2"),
            observed_at=NOW + timedelta(seconds=6),
            complete=True,
            legs=tuple(
                LegReconciliation(
                    leg.role,
                    leg.cloid,
                    "canceled",
                    leg.cumulative_filled,
                    leg.venue_oid,
                )
                for leg in updated_legs
            ),
            signed_position_quantity="0",
            protected_quantity="0",
        )
        self.assertEqual("terminal", terminal.state)
        self.assertEqual((Decimal("0"), Decimal("0")), self.store.get_reserved_exposure())
        self.assertEqual("terminal", self.store.get_outbox("command-1").state)
        self.assertEqual("flat", self.store.get_protection("command-1").state)

    def test_filled_entry_rejected_stop_opens_critical_failed_protection(self) -> None:
        ticket, claim, signed = self._prepared_response_command()
        response = parse_order_response(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": str(ticket.quantity),
                                    "avgPx": "2500",
                                    "oid": 1,
                                }
                            },
                            {"error": "stop rejected"},
                            {"resting": {"oid": 3}},
                        ]
                    },
                },
            },
            requested_sizes=(ticket.quantity,) * 3,
        )
        command = self.store.record_submission_response(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            response,
            transport_evidence=self.make_transport_evidence(
                "attempt-1",
                signed,
                outcome="response_received",
                response_hash=digest("transport-filled-stop-rejected"),
            ),
            at=NOW + timedelta(seconds=3),
        )
        self.assertEqual("reconciling", command.state)
        self.assertEqual("failed", self.store.get_protection("command-1").state)
        incidents = self.store.list_incidents("command-1")
        self.assertIn("PROTECTION_SUBMISSION_FAILED", {item.code for item in incidents})
        self.assertTrue(all(item.severity == "critical" for item in incidents))
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])
        self.assertEqual(
            "response_received",
            self.store.get_transport_evidence("command-1").outcome,
        )

    def test_partial_entry_opens_critical_under_protected_incident(self) -> None:
        ticket, claim, signed = self._prepared_response_command()
        partial = ticket.quantity / Decimal("2")
        response = parse_order_response(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "filled": {
                                    "totalSz": str(partial),
                                    "avgPx": "2500",
                                    "oid": 1,
                                }
                            },
                            {"resting": {"oid": 2}},
                            {"resting": {"oid": 3}},
                        ]
                    },
                },
            },
            requested_sizes=(ticket.quantity,) * 3,
        )
        self.store.record_submission_response(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            response,
            transport_evidence=self.make_transport_evidence(
                "attempt-1",
                signed,
                outcome="response_received",
                response_hash=digest("transport-partial"),
            ),
            at=NOW + timedelta(seconds=3),
        )
        protection = self.store.get_protection("command-1")
        self.assertEqual("under_protected", protection.state)
        self.assertEqual(partial, abs(protection.signed_position_quantity))
        incidents = self.store.list_incidents("command-1")
        self.assertIn("ENTRY_PARTIAL_FILL", {item.code for item in incidents})
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])

    def test_incident_state_is_cas_and_event_chain_is_valid(self) -> None:
        self.admit_one()
        incident = self.store.record_incident(
            incident_id="incident-1",
            command_id="command-1",
            code="TEST_INCIDENT",
            severity="warning",
            at=NOW + timedelta(seconds=1),
            details={"reason": "fixture"},
        )
        contained = self.store.update_incident_state(
            incident.incident_id,
            expected_revision=incident.revision,
            state="contained",
            at=NOW + timedelta(seconds=2),
        )
        with self.assertRaises(StateConflict):
            self.store.update_incident_state(
                incident.incident_id,
                expected_revision=incident.revision,
                state="closed",
                at=NOW + timedelta(seconds=3),
            )
        closed = self.store.update_incident_state(
            incident.incident_id,
            expected_revision=contained.revision,
            state="closed",
            at=NOW + timedelta(seconds=3),
        )
        self.assertEqual("closed", closed.state)
        self.assertTrue(self.store.verify_event_chain())


class TamperDetectionTests(ExecutionStoreTestCase):
    def test_signed_and_transport_evidence_tamper_are_detected(self) -> None:
        self.prepare_unknown()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_signed_envelopes SET signature_hash = ?",
                (digest("tampered-signature"),),
            )
            connection.execute(
                "UPDATE execution_transport_outcomes SET detail_code = 'tampered'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_signed_evidence("command-1")
        with self.assertRaises(StorageError):
            self.store.get_transport_evidence("command-1")

    def test_plan_leg_tamper_is_detected_before_approval_consumption(self) -> None:
        ticket, approval = self.register_approve()
        assert ticket.plan is not None
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                UPDATE execution_plan_legs SET quantity = '999'
                WHERE plan_hash = ? AND role = 'entry'
                """,
                (ticket.plan.plan_hash,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.admit(
                command_id="tampered-command",
                approval_id=approval.approval_id,
                token_hash=approval.token_hash,
                audience=approval.audience,
                at=NOW + timedelta(seconds=1),
            )
        self.assertEqual("issued", self.store.approval_state(approval.approval_id))

    def test_command_and_outbox_record_tamper_are_detected(self) -> None:
        self.admit_one()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_commands SET state = 'terminal' WHERE command_id = 'command-1'"
            )
            connection.execute(
                "UPDATE execution_outbox SET worker_id = 'intruder' WHERE command_id = 'command-1'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_command("command-1")
        with self.assertRaises(StorageError):
            self.store.get_outbox("command-1")

    def test_command_outbox_attempt_and_event_tamper_are_detected(self) -> None:
        ticket, _ = self.admit_one()
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        preflight = self.register_preflight(ticket)
        signed = self.make_signed_evidence(preflight, nonce=123)
        self.store.prepare_attempt(
            "command-1",
            "dispatcher",
            claim.fencing_token,
            attempt_id="attempt-1",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=123,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_attempts SET wire_hash = ? WHERE attempt_id = 'attempt-1'",
                (digest("tampered"),),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.get_attempt("command-1")

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE execution_events SET payload_json = '{}' WHERE event_sequence = 1"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.verify_event_chain()

    def test_failed_duplicate_command_rolls_back_approval_consumption(self) -> None:
        _, first = self.admit_one(command_id="duplicate")
        self.store.void_unsent_command(
            "duplicate",
            reason="fixture allows another command admission",
            at=NOW + timedelta(milliseconds=4),
        )
        second_ticket = make_ticket(
            "ticket-2", instrument="SOL-PERP", symbol="SOL"
        )
        self.store.register_ticket(
            second_ticket, stored_at=NOW + timedelta(milliseconds=1)
        )
        second = make_approval(second_ticket, "approval-2", token_text="token-2")
        self.store.register_approval(second)
        with self.assertRaises(StateConflict):
            self.store.admit(
                command_id="duplicate",
                approval_id=second.approval_id,
                token_hash=second.token_hash,
                audience=second.audience,
                at=NOW + timedelta(seconds=1),
            )
        self.assertEqual("consumed", self.store.approval_state(first.approval_id))
        self.assertEqual("issued", self.store.approval_state(second.approval_id))


if __name__ == "__main__":
    unittest.main()
