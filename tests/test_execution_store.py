from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import trading_harness.execution_store as execution_store_module

from trading_harness.analysis import TechnicalBias, TechnicalSnapshot
from trading_harness.canonical import canonical_json, domain_hash
from trading_harness.assessment import (
    ProfitabilityGate,
    ProfitabilityStatus,
    build_opportunity_assessment,
)
from trading_harness.domain import Environment
from trading_harness.errors import (
    AdmissionDenied,
    PolicyViolation,
    RecordNotFound,
    StateConflict,
    StorageError,
    ValidationError,
)
from trading_harness.execution_store import (
    DispatchPreflight,
    ExecutionStore,
    LegReconciliation,
    NoopFenceResponseEvidence,
    RecoveryPermit,
    RecoveryReconciliationProof,
    SignedEnvelopeEvidence,
    SignedRecoveryEvidence,
    TransportOutcomeEvidence,
    TrustedApproval,
    VenueFill,
)
from trading_harness.execution_grant import TrustedInfrastructureGrant
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


def make_infrastructure_grant(
    ticket: RiskTicket,
    *,
    grant_id: str = "infrastructure-grant-1",
    account_id: str = "testnet-account",
) -> TrustedInfrastructureGrant:
    assert ticket.plan is not None
    return TrustedInfrastructureGrant(
        grant_hash=digest(f"grant:{grant_id}:{ticket.policy_hash}:{account_id}"),
        grant_id=grant_id,
        generation=1,
        account_id=account_id,
        environment=Environment.TESTNET,
        allowed_instruments=(ticket.plan.entry.instrument,),
        risk_policy_hash=ticket.policy_hash,
        max_loss=Decimal("100"),
        max_notional=Decimal("2000"),
        max_leverage=Decimal("2"),
        issuer_id="test-learning-authority",
        audience="test-executor",
        issued_at=NOW - timedelta(seconds=1),
        not_before=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(hours=1),
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
        grant = make_infrastructure_grant(ticket)
        self.store.register_infrastructure_grant(grant, at=NOW)
        self.store.register_ticket(
            ticket,
            infrastructure_grant_hash=grant.grant_hash,
            stored_at=NOW + timedelta(milliseconds=1),
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
            account_server_time_ms=int(
                (observed_at - timedelta(milliseconds=500)).timestamp() * 1_000
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
            endpoint=getattr(
                signed,
                "endpoint",
                "https://api.hyperliquid-testnet.xyz/exchange",
            ),
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

    def recovery_parent(
        self,
        *,
        unknown: bool = False,
        incident_id: str = "recovery-incident",
    ):
        if unknown:
            self.prepare_unknown()
        else:
            self.admit_one()
        incident = self.store.record_incident(
            incident_id=incident_id,
            command_id="command-1",
            code="RECOVERY_REQUIRED",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )
        attempt = self.store.get_attempt("command-1") if unknown else None
        return incident, attempt

    def make_recovery_permit(
        self,
        *,
        kind: str,
        incident_id: str = "recovery-incident",
        permit_id: str | None = None,
        attempt=None,
    ) -> RecoveryPermit:
        if kind == "reduce_only_close":
            recovery_material = {
                "kind": kind,
                "original_signed_position": "1",
                "close_size": "1",
                "action": {"type": "order"},
            }
        elif kind == "cancel_by_cloid":
            recovery_material = {
                "kind": kind,
                "requests": [{"cloid": "0x" + "d" * 32}],
                "action": {"type": "cancelByCloid"},
            }
        else:
            recovery_material = {
                "kind": kind,
                "attempt_id": None if attempt is None else attempt.attempt_id,
                "preflight_hash": None if attempt is None else attempt.preflight_hash,
                "original_nonce": None if attempt is None else attempt.nonce,
                "action": {"type": "noop"},
            }
        recovery_hash = domain_hash(
            "trading-harness/hyperliquid-recovery-action/v1",
            recovery_material,
        )
        return RecoveryPermit(
            permit_id=permit_id or f"permit-{kind}",
            token_hash=digest(f"token-{permit_id or kind}"),
            parent_command_id="command-1",
            incident_id=incident_id,
            kind=kind,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            source_hash=digest(f"source-{kind}"),
            preflight_hash=(None if attempt is None else attempt.preflight_hash),
            recovery_hash=recovery_hash,
            recovery_material=recovery_material,
            safety_policy_hash=digest("account-safety-policy"),
            original_attempt_id=(None if attempt is None else attempt.attempt_id),
            original_nonce=(None if attempt is None else attempt.nonce),
            issuer_id="safety-authority",
            audience="recovery-worker",
            issued_at=NOW + timedelta(seconds=6),
            expires_at=NOW + timedelta(seconds=16),
        )

    def queue_recovery_fixture(
        self,
        *,
        kind: str = "reduce_only_close",
        unknown: bool = False,
        incident_id: str = "recovery-incident",
        recovery_command_id: str | None = None,
    ):
        incident, attempt = self.recovery_parent(
            unknown=unknown, incident_id=incident_id
        )
        permit = self.make_recovery_permit(
            kind=kind, incident_id=incident_id, attempt=attempt
        )
        self.store.register_recovery_permit(permit)
        command = self.store.queue_recovery(
            recovery_command_id=(
                recovery_command_id or f"recovery-command-{kind}"
            ),
            permit_id=permit.permit_id,
            token_hash=permit.token_hash,
            audience=permit.audience,
            at=NOW + timedelta(seconds=7),
        )
        return incident, permit, command

    def make_signed_recovery(
        self,
        command,
        *,
        signing_authority_hash: str,
        nonce: int = 888,
    ) -> SignedRecoveryEvidence:
        return SignedRecoveryEvidence(
            recovery_command_id=command.recovery_command_id,
            incident_id=command.incident_id,
            kind=command.kind,
            source_hash=command.source_hash,
            recovery_hash=command.recovery_hash,
            signing_authority_hash=signing_authority_hash,
            safety_policy_hash=command.safety_policy_hash,
            nonce=nonce,
            wire_hash=digest(f"wire-{command.recovery_command_id}"),
            action_hash=digest(f"action-{command.recovery_command_id}"),
            signature_hash=digest("recovery-signature"),
            envelope_hash=digest("recovery-envelope"),
            signer_binding_hash=digest("recovery-signer-binding"),
            expires_after_ms=int(
                (NOW + timedelta(seconds=15)).timestamp() * 1_000
            ),
            signed_at_ms=int((NOW + timedelta(seconds=8)).timestamp() * 1_000),
        )

    def make_recovery_proof(
        self,
        command,
        *,
        observed_at: datetime,
        complete: bool,
        success: bool,
    ) -> RecoveryReconciliationProof:
        affected = (
            ("0x" + "d" * 32,)
            if command.kind == "cancel_by_cloid"
            else ()
        )
        return RecoveryReconciliationProof(
            recovery_command_id=command.recovery_command_id,
            kind=command.kind,
            account_snapshot_hash=digest(
                f"recovery-account-{command.recovery_command_id}-{observed_at}"
            ),
            observed_at=observed_at,
            signed_position_quantity=Decimal("0"),
            protected_quantity=Decimal("0"),
            open_order_cloids=(),
            affected_cloids=affected,
            resolved_original_nonce=(
                command.original_nonce
                if command.kind == "noop_fence" and success
                else None
            ),
            resolved_original_outcome=(
                "fenced" if command.kind == "noop_fence" and success else None
            ),
            complete=complete,
            success=success,
        )


class MigrationAndIdentityTests(ExecutionStoreTestCase):
    @staticmethod
    def _file_contents(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_existing_only_rejects_invalid_files_without_mutation(self) -> None:
        fixtures: list[tuple[str, Path]] = []

        missing_root = Path(self.temporary.name) / "missing"
        missing_root.mkdir()
        fixtures.append(("missing", missing_root / "execution.sqlite3"))

        zero_root = Path(self.temporary.name) / "zero-byte"
        zero_root.mkdir()
        zero_path = zero_root / "execution.sqlite3"
        zero_path.touch()
        fixtures.append(("zero-byte", zero_path))

        empty_root = Path(self.temporary.name) / "schema-less"
        empty_root.mkdir()
        empty_path = empty_root / "execution.sqlite3"
        connection = sqlite3.connect(empty_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("VACUUM")
        finally:
            connection.close()
        fixtures.append(("schema-less", empty_path))

        wrong_root = Path(self.temporary.name) / "wrong-store"
        wrong_root.mkdir()
        wrong_path = wrong_root / "execution.sqlite3"
        SQLiteStore(wrong_path)
        fixtures.append(("wrong-store", wrong_path))

        symlink_root = Path(self.temporary.name) / "symlink"
        symlink_root.mkdir()
        symlink_path = symlink_root / "execution.sqlite3"
        symlink_path.symlink_to(self.path)
        fixtures.append(("symlink", symlink_path))

        hardlink_root = Path(self.temporary.name) / "hardlink"
        hardlink_root.mkdir()
        hardlink_path = hardlink_root / "execution.sqlite3"
        os.link(self.path, hardlink_path)
        fixtures.append(("hardlink", hardlink_path))

        for name, path in fixtures:
            with self.subTest(name=name):
                before = self._file_contents(path.parent)
                with self.assertRaises(StorageError):
                    ExecutionStore(
                        path,
                        environment=Environment.TESTNET,
                        account_id="testnet-account",
                        max_reserved_loss="100",
                        max_reserved_notional="2000",
                        must_exist=True,
                    )
                self.assertEqual(before, self._file_contents(path.parent))

    def test_existing_only_valid_reopen_is_read_only_during_verification(self) -> None:
        before = self._file_contents(self.path.parent)
        reopened = ExecutionStore(
            self.path,
            environment=Environment.TESTNET,
            account_id="testnet-account",
            max_reserved_loss="100",
            max_reserved_notional="2000",
            must_exist=True,
        )
        self.assertEqual(before, self._file_contents(self.path.parent))
        self.assertEqual(
            (Decimal("0"), Decimal("0")), reopened.get_reserved_exposure()
        )

    def test_existing_only_rejects_extra_trigger_without_mutation(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER stealth_execution_trigger
                BEFORE UPDATE ON execution_exposure
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
        before = self._file_contents(self.path.parent)

        with self.assertRaisesRegex(StorageError, "schema does not match"):
            ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
                must_exist=True,
            )

        self.assertEqual(before, self._file_contents(self.path.parent))

    def test_existing_only_rejects_foreign_key_violation_without_mutation(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO execution_plan_legs (
                    plan_hash, role, cloid, intent_hash, side, reduce_only,
                    quantity, price_bound, payload_json, content_hash
                ) VALUES (?, 'entry', ?, ?, 'buy', 0, '1', '1', '{}', ?)
                """,
                ("f" * 64, "0x" + "1" * 32, "a" * 64, "b" * 64),
            )
        before = self._file_contents(self.path.parent)

        with self.assertRaisesRegex(StorageError, "foreign keys"):
            ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
                must_exist=True,
            )

        self.assertEqual(before, self._file_contents(self.path.parent))

    def test_existing_only_verifies_committed_state_retained_in_wal(self) -> None:
        reader = sqlite3.connect(self.path)
        try:
            reader.execute("BEGIN")
            reader.execute(
                "SELECT singleton FROM execution_exposure WHERE singleton = 1"
            ).fetchone()
            updated_at = NOW + timedelta(seconds=1)
            exposure_payload = {
                "reserved_loss": "1",
                "reserved_notional": "10",
                "revision": 2,
                "updated_at": execution_store_module._time_text(
                    updated_at, field="updated_at"
                ),
            }
            writer = sqlite3.connect(self.path)
            try:
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    """
                    UPDATE execution_exposure SET
                        reserved_loss = '1', reserved_notional = '10',
                        revision = 2, updated_at = ?, record_hash = ?
                    WHERE singleton = 1
                    """,
                    (
                        exposure_payload["updated_at"],
                        execution_store_module._record_hash(
                            "exposure", exposure_payload
                        ),
                    ),
                )
                writer.commit()
            finally:
                writer.close()
            self.assertGreater(Path(f"{self.path}-wal").stat().st_size, 0)
            before = self._file_contents(self.path.parent)
            reopened = ExecutionStore(
                self.path,
                environment=Environment.TESTNET,
                account_id="testnet-account",
                max_reserved_loss="100",
                max_reserved_notional="2000",
                must_exist=True,
            )
            self.assertEqual(before, self._file_contents(self.path.parent))
        finally:
            reader.close()
        self.assertEqual(
            (Decimal("1"), Decimal("10")), reopened.get_reserved_exposure()
        )

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
        self.assertEqual(
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            [row[0] for row in migrations],
        )
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
                mainnet_ticket,
                infrastructure_grant_hash="0" * 64,
                stored_at=NOW + timedelta(milliseconds=1),
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
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], versions)
        self.assertIn("preflight_hash", columns)
        self.assertIn("signed_evidence_hash", columns)
        self.assertIn("transport_evidence_hash", columns)
        self.assertIsNotNone(preflight_table)


class TicketApprovalAdmissionTests(ExecutionStoreTestCase):
    def test_ticket_requires_registered_active_matching_learning_grant(self) -> None:
        ticket = make_ticket("grant-required-ticket")
        with self.assertRaises(TypeError):
            self.store.register_ticket(  # type: ignore[call-arg]
                ticket,
                stored_at=NOW + timedelta(milliseconds=1),
            )
        with self.assertRaises(RecordNotFound):
            self.store.register_ticket(
                ticket,
                infrastructure_grant_hash="0" * 64,
                stored_at=NOW + timedelta(milliseconds=1),
            )

        wrong_policy = replace(
            make_infrastructure_grant(ticket, grant_id="wrong-policy-grant"),
            risk_policy_hash=digest("wrong-policy"),
        )
        self.store.register_infrastructure_grant(wrong_policy, at=NOW)
        with self.assertRaises(PolicyViolation):
            self.store.register_ticket(
                ticket,
                infrastructure_grant_hash=wrong_policy.grant_hash,
                stored_at=NOW + timedelta(milliseconds=1),
            )

        too_small = replace(
            make_infrastructure_grant(ticket, grant_id="small-grant"),
            max_loss=Decimal("0.01"),
        )
        self.store.register_infrastructure_grant(too_small, at=NOW)
        with self.assertRaises(PolicyViolation):
            self.store.register_ticket(
                ticket,
                infrastructure_grant_hash=too_small.grant_hash,
                stored_at=NOW + timedelta(milliseconds=1),
            )

        approval = make_approval(ticket, "orphan-approval")
        with self.assertRaises(RecordNotFound):
            self.store.register_approval(approval)

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
                ticket,
                infrastructure_grant_hash=make_infrastructure_grant(ticket).grant_hash,
                stored_at=NOW + timedelta(seconds=1),
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
            grant = make_infrastructure_grant(
                ticket,
                grant_id=f"capped-grant-{index}",
            )
            store.register_infrastructure_grant(grant, at=NOW)
            store.register_ticket(
                ticket,
                infrastructure_grant_hash=grant.grant_hash,
                stored_at=NOW + timedelta(milliseconds=1),
            )
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
        claim = self.store.claim_next(
            "dispatcher", at=NOW + timedelta(seconds=1), lease_seconds=10
        )
        assert claim is not None
        command = self.store.void_unsent_command(
            "command-1",
            reason="signer failed before attempt persistence",
            at=NOW + timedelta(seconds=2),
            worker_id="dispatcher",
            fencing_token=claim.fencing_token,
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

    def test_stale_worker_cannot_void_replacement_claim(self) -> None:
        self.admit_one()
        stale = self.store.claim_next(
            "stale-worker", at=NOW + timedelta(seconds=1), lease_seconds=5
        )
        assert stale is not None
        replacement = self.store.claim_next(
            "replacement-worker",
            at=NOW + timedelta(seconds=6),
            lease_seconds=5,
        )
        assert replacement is not None
        with self.assertRaisesRegex(StateConflict, "stale"):
            self.store.void_unsent_command(
                "command-1",
                reason="stale preflight denial",
                at=NOW + timedelta(seconds=6, milliseconds=1),
                worker_id="stale-worker",
                fencing_token=stale.fencing_token,
            )
        current = self.store.get_outbox("command-1")
        self.assertEqual("replacement-worker", current.worker_id)
        self.assertEqual(replacement.fencing_token, current.fencing_token)

        terminal = self.store.void_unsent_command(
            "command-1",
            reason="replacement denial",
            at=NOW + timedelta(seconds=6, milliseconds=2),
            worker_id="replacement-worker",
            fencing_token=replacement.fencing_token,
        )
        self.assertEqual("terminal", terminal.state)

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
            grant = make_infrastructure_grant(ticket)
            self.store.register_infrastructure_grant(grant, at=NOW)
            self.store.register_ticket(
                ticket,
                infrastructure_grant_hash=grant.grant_hash,
                stored_at=NOW + timedelta(milliseconds=1),
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
        self.store.require_submission_authority(
            "command-1",
            attempt.attempt_id,
            signed.evidence_hash,
            "dispatcher",
            claim.fencing_token,
            at=NOW + timedelta(seconds=2, milliseconds=1),
        )
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

    def test_prepared_entry_without_submission_authority_expires_proven_unsent(self) -> None:
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
            attempt_id="prepared-never-authorized",
            preflight_hash=preflight.preflight_hash,
            signed_evidence=signed,
            nonce=1_777_777_777_777,
            action_hash=digest("action"),
            wire_hash=digest("wire"),
            at=NOW + timedelta(seconds=2),
        )

        self.assertIsNone(
            self.store.claim_next(
                "replacement", at=NOW + timedelta(seconds=6), lease_seconds=5
            )
        )
        self.assertEqual("terminal", self.store.get_command("command-1").state)
        self.assertEqual("terminal", self.store.get_outbox("command-1").state)
        self.assertEqual("prepared", self.store.get_attempt("command-1").state)
        self.assertEqual(attempt.attempt_id, self.store.get_outbox("command-1").current_attempt_id)
        self.assertEqual((Decimal("0"), Decimal("0")), self.store.get_reserved_exposure())
        with self.assertRaises(RecordNotFound):
            self.store.get_transport_evidence("command-1")

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

    def test_entry_submission_authority_is_exact_and_single_use(self) -> None:
        _, claim, signed = self._prepared_response_command()

        authority = self.store.require_submission_authority(
            "command-1",
            "attempt-1",
            signed.evidence_hash,
            "dispatcher",
            claim.fencing_token,
            at=NOW + timedelta(seconds=2, milliseconds=100),
        )

        self.assertEqual("command-1", authority.command_id)
        self.assertEqual("attempt-1", authority.attempt_id)
        self.assertEqual(123, authority.nonce)
        self.assertRegex(authority.authority_hash, r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(StateConflict, "already consumed"):
            self.store.require_submission_authority(
                "command-1",
                "attempt-1",
                signed.evidence_hash,
                "dispatcher",
                claim.fencing_token,
                at=NOW + timedelta(seconds=2, milliseconds=200),
            )

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
        released_parent_claim = self.store.get_outbox("command-1")
        self.assertIsNone(released_parent_claim.worker_id)
        self.assertIsNone(released_parent_claim.lease_expires_at)
        self.assertEqual(ticket.stressed_loss, self.store.get_reserved_exposure()[0])
        self.assertEqual(half, self.store.get_position("ETH-PERP").signed_quantity)
        self.assertEqual("under_protected", self.store.get_protection("command-1").state)
        self.assertEqual(
            (replace(fill, observed_at=NOW + timedelta(seconds=5)),),
            self.store.list_fills("command-1"),
        )
        incidents = self.store.list_incidents("command-1")
        self.assertIn("POSITION_UNDER_PROTECTED", {item.code for item in incidents})
        self.assertTrue(
            all(
                item.severity == "critical"
                for item in incidents
                if item.code == "POSITION_UNDER_PROTECTED"
            )
        )
        ambiguity = self.store.record_incident(
            incident_id="unknown-submission-ambiguity",
            command_id="command-1",
            code="UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING",
            severity="critical",
            at=NOW + timedelta(seconds=5, milliseconds=250),
            details={"requires_same_nonce_fence": True},
        )
        unrelated = self.store.record_incident(
            incident_id="unrelated-open-critical",
            command_id="command-1",
            code="UNRELATED_OPEN_CRITICAL",
            severity="critical",
            at=NOW + timedelta(seconds=5, milliseconds=300),
        )

        updated_legs = self.store.get_legs("command-1")
        next_claim = self.store.claim_reconciliation(
            "command-1",
            "reconciler",
            at=NOW + timedelta(seconds=5, milliseconds=500),
            lease_seconds=30,
        )
        terminal = self.store.reconcile(
            "command-1",
            "reconciler",
            next_claim.fencing_token,
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
        incidents_by_id = {
            item.incident_id: item
            for item in self.store.list_incidents("command-1")
        }
        self.assertEqual("closed", incidents_by_id[ambiguity.incident_id].state)
        self.assertEqual("open", incidents_by_id[unrelated.incident_id].state)
        self.assertTrue(self.store.verify_event_chain())

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


class RecoveryLifecycleTests(ExecutionStoreTestCase):
    def test_expired_queued_recovery_terminalizes_unsent_and_allows_replacement(self) -> None:
        incident, permit, command = self.queue_recovery_fixture()

        claimed = self.store.claim_next_recovery(
            "late-worker",
            at=permit.expires_at,
            lease_seconds=10,
        )

        self.assertIsNone(claimed)
        self.assertEqual(
            "terminal",
            self.store.get_recovery_command(command.recovery_command_id).state,
        )
        self.assertEqual(
            "terminal",
            self.store.get_recovery_outbox(command.recovery_command_id).state,
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_attempt(command.recovery_command_id)
        self.assertEqual("open", self.store.list_incidents("command-1")[0].state)

        replacement = self.make_recovery_permit(
            kind="reduce_only_close",
            incident_id=incident.incident_id,
            permit_id="replacement-close-permit",
        )
        replacement = replace(
            replacement,
            issued_at=permit.expires_at,
            expires_at=permit.expires_at + timedelta(seconds=10),
        )
        self.store.register_recovery_permit(replacement)
        queued = self.store.queue_recovery(
            recovery_command_id="replacement-close-command",
            permit_id=replacement.permit_id,
            token_hash=replacement.token_hash,
            audience=replacement.audience,
            at=permit.expires_at + timedelta(milliseconds=1),
        )
        self.assertEqual("queued", queued.state)

    def test_permit_is_single_use_exact_and_recovery_blocks_entry_dispatch(self) -> None:
        _, permit, command = self.queue_recovery_fixture()
        self.assertEqual("consumed", self.store.get_recovery_permit_state(permit.permit_id))
        self.assertEqual("queued", command.state)
        with self.assertRaises(StateConflict):
            self.store.claim_next(
                "entry-dispatcher",
                at=NOW + timedelta(seconds=8),
                lease_seconds=5,
            )
        with self.assertRaises(AdmissionDenied):
            self.store.queue_recovery(
                recovery_command_id="replay-recovery",
                permit_id=permit.permit_id,
                token_hash=permit.token_hash,
                audience=permit.audience,
                at=NOW + timedelta(seconds=8),
            )

    def test_permit_rejects_foreign_expired_and_noncritical_incident(self) -> None:
        self.admit_one()
        warning = self.store.record_incident(
            incident_id="warning-incident",
            command_id="command-1",
            code="WARNING",
            severity="warning",
            at=NOW + timedelta(seconds=5),
        )
        permit = self.make_recovery_permit(
            kind="reduce_only_close", incident_id=warning.incident_id
        )
        with self.assertRaises(StateConflict):
            self.store.register_recovery_permit(permit)
        critical = self.store.record_incident(
            incident_id="critical-incident",
            command_id="command-1",
            code="CRITICAL",
            severity="critical",
            at=NOW + timedelta(seconds=5),
        )
        foreign = replace(
            self.make_recovery_permit(
                kind="reduce_only_close", incident_id=critical.incident_id
            ),
            account_id="foreign-account",
        )
        with self.assertRaises(ValidationError):
            self.store.register_recovery_permit(foreign)
        expired = replace(
            self.make_recovery_permit(
                kind="reduce_only_close",
                incident_id=critical.incident_id,
                permit_id="expired-permit",
            ),
            issued_at=NOW + timedelta(seconds=5),
            expires_at=NOW + timedelta(seconds=6),
        )
        self.store.register_recovery_permit(expired)
        with self.assertRaises(AdmissionDenied) as caught:
            self.store.queue_recovery(
                recovery_command_id="expired-recovery",
                permit_id=expired.permit_id,
                token_hash=expired.token_hash,
                audience=expired.audience,
                at=NOW + timedelta(seconds=6),
            )
        self.assertEqual("RECOVERY_PERMIT_EXPIRED", caught.exception.code)

    def test_recovery_commands_are_serialized_account_wide(self) -> None:
        self.admit_one()
        incidents = []
        for value in ("close", "cancel"):
            incidents.append(
                self.store.record_incident(
                    incident_id=f"incident-{value}",
                    command_id="command-1",
                    code=f"RECOVERY_{value.upper()}",
                    severity="critical",
                    at=NOW + timedelta(seconds=5),
                )
            )
        close = self.make_recovery_permit(
            kind="reduce_only_close",
            incident_id=incidents[0].incident_id,
            permit_id="permit-close",
        )
        cancel = self.make_recovery_permit(
            kind="cancel_by_cloid",
            incident_id=incidents[1].incident_id,
            permit_id="permit-cancel",
        )
        self.store.register_recovery_permit(close)
        command = self.store.queue_recovery(
            recovery_command_id="recovery-close",
            permit_id=close.permit_id,
            token_hash=close.token_hash,
            audience=close.audience,
            at=NOW + timedelta(seconds=7),
        )
        self.assertEqual(2, command.priority)
        self.store.register_recovery_permit(cancel)
        with self.assertRaises(StateConflict):
            self.store.queue_recovery(
                recovery_command_id="recovery-cancel",
                permit_id=cancel.permit_id,
                token_hash=cancel.token_hash,
                audience=cancel.audience,
                at=NOW + timedelta(seconds=7),
            )

    def test_unknown_attempt_requires_terminal_noop_before_close(self) -> None:
        incident, attempt = self.recovery_parent(unknown=True)
        assert attempt is not None
        close = self.make_recovery_permit(
            kind="reduce_only_close", incident_id=incident.incident_id
        )
        with self.assertRaises(StateConflict):
            self.store.register_recovery_permit(close)
        noop = self.make_recovery_permit(
            kind="noop_fence", incident_id=incident.incident_id, attempt=attempt
        )
        self.store.register_recovery_permit(noop)
        command = self.store.queue_recovery(
            recovery_command_id="recovery-noop",
            permit_id=noop.permit_id,
            token_hash=noop.token_hash,
            audience=noop.audience,
            at=NOW + timedelta(seconds=7),
        )
        claim = self.store.claim_next_recovery(
            "recovery-worker", at=NOW + timedelta(seconds=8), lease_seconds=10
        )
        assert claim is not None
        signing_authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        self.assertEqual(attempt.nonce, signing_authority.original_nonce)
        wrong = self.make_signed_recovery(
            command,
            signing_authority_hash=signing_authority.authority_hash,
            nonce=attempt.nonce + 1,
        )
        with self.assertRaises(StateConflict):
            self.store.prepare_recovery_attempt(
                command.recovery_command_id,
                "recovery-worker",
                claim.fencing_token,
                attempt_id="noop-attempt-wrong",
                signed_evidence=wrong,
                at=NOW + timedelta(seconds=9),
            )
        signed = self.make_signed_recovery(
            command,
            signing_authority_hash=signing_authority.authority_hash,
            nonce=attempt.nonce,
        )
        recovery_attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="noop-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        submission_authority = self.store.require_recovery_submission_authority(
            command.recovery_command_id,
            recovery_attempt.attempt_id,
            signed.evidence_hash,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        self.assertEqual(signed.nonce, submission_authority.nonce)
        noop_body = {"status": "ok", "response": {"type": "default"}}
        noop_response_hash = domain_hash(
            "trading-harness/hyperliquid-submission-response/v1",
            noop_body,
        )
        transport = self.make_transport_evidence(
            recovery_attempt.attempt_id,
            signed,
            command_id=command.recovery_command_id,
            outcome="response_received",
            response_hash=noop_response_hash,
        )
        noop_response = NoopFenceResponseEvidence(
            recovery_command_id=command.recovery_command_id,
            attempt_id=recovery_attempt.attempt_id,
            signed_evidence_hash=signed.evidence_hash,
            transport_evidence_hash=transport.evidence_hash,
            nonce=signed.nonce,
            response_json=canonical_json(noop_body),
            response_hash=noop_response_hash,
            parsed_at=NOW + timedelta(seconds=10),
        )
        self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            noop_response=noop_response,
            at=NOW + timedelta(seconds=10),
        )
        self.assertEqual(
            noop_response,
            self.store.get_noop_fence_response(command.recovery_command_id),
        )
        recon_claim = self.store.claim_recovery_reconciliation(
            command.recovery_command_id,
            "reconciler",
            at=NOW + timedelta(seconds=11),
            lease_seconds=10,
        )
        self.store.reconcile_recovery(
            command.recovery_command_id,
            "reconciler",
            recon_claim.fencing_token,
            reconciliation_id="noop-reconciliation",
            proof=self.make_recovery_proof(
                command,
                observed_at=NOW + timedelta(seconds=12),
                complete=True,
                success=True,
            ),
            incident_resolution=None,
        )
        close_after_fence = replace(
            close,
            permit_id="permit-close-after-fence",
            token_hash=digest("close-after-fence"),
        )
        self.store.register_recovery_permit(close_after_fence)

    def test_recovery_attempt_response_and_reconciliation_retain_parent_risk(self) -> None:
        incident, permit, command = self.queue_recovery_fixture()
        claim = self.store.claim_next_recovery(
            "recovery-worker", at=NOW + timedelta(seconds=8), lease_seconds=10
        )
        assert claim is not None
        signing_authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        self.assertEqual(command.recovery_hash, signing_authority.recovery_hash)
        with self.assertRaises(StateConflict):
            self.store.require_recovery_signing_authority(
                command.recovery_command_id,
                "recovery-worker",
                claim.fencing_token,
                at=NOW + timedelta(seconds=8, milliseconds=2),
            )
        signed = self.make_signed_recovery(
            command,
            signing_authority_hash=signing_authority.authority_hash,
        )
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="recovery-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        self.store.require_recovery_submission_authority(
            command.recovery_command_id,
            attempt.attempt_id,
            signed.evidence_hash,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        with self.assertRaises(StateConflict):
            self.store.require_recovery_submission_authority(
                command.recovery_command_id,
                attempt.attempt_id,
                signed.evidence_hash,
                "recovery-worker",
                claim.fencing_token,
                at=NOW + timedelta(seconds=9, milliseconds=2),
            )
        transport = self.make_transport_evidence(
            attempt.attempt_id,
            signed,
            command_id=command.recovery_command_id,
            outcome="response_received",
            response_hash=digest("recovery-response"),
        )
        state = self.store.record_recovery_outcome(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            transport_evidence=transport,
            at=NOW + timedelta(seconds=10),
        )
        self.assertEqual("reconciling", state.state)
        self.assertEqual(
            transport,
            self.store.get_recovery_transport_evidence(
                command.recovery_command_id
            ),
        )
        recon_claim = self.store.claim_recovery_reconciliation(
            command.recovery_command_id,
            "reconciler",
            at=NOW + timedelta(seconds=11),
            lease_seconds=10,
        )
        incomplete = self.store.reconcile_recovery(
            command.recovery_command_id,
            "reconciler",
            recon_claim.fencing_token,
            reconciliation_id="recovery-incomplete",
            proof=self.make_recovery_proof(
                command,
                observed_at=NOW + timedelta(seconds=12),
                complete=False,
                success=False,
            ),
            incident_resolution=None,
        )
        self.assertEqual("reconciling", incomplete.state)
        released_recovery_claim = self.store.get_recovery_outbox(
            command.recovery_command_id
        )
        self.assertIsNone(released_recovery_claim.worker_id)
        self.assertIsNone(released_recovery_claim.lease_expires_at)
        next_recon_claim = self.store.claim_recovery_reconciliation(
            command.recovery_command_id,
            "reconciler",
            at=NOW + timedelta(seconds=12, milliseconds=250),
            lease_seconds=10,
        )
        adversarial = replace(
            self.make_recovery_proof(
                command,
                observed_at=NOW + timedelta(seconds=12, milliseconds=500),
                complete=True,
                success=True,
            ),
            signed_position_quantity=Decimal("999"),
            proof_hash="",
        )
        with self.assertRaises(StateConflict):
            self.store.reconcile_recovery(
                command.recovery_command_id,
                "reconciler",
                next_recon_claim.fencing_token,
                reconciliation_id="recovery-adversarial",
                proof=adversarial,
                incident_resolution=None,
            )
        terminal = self.store.reconcile_recovery(
            command.recovery_command_id,
            "reconciler",
            next_recon_claim.fencing_token,
            reconciliation_id="recovery-complete",
            proof=self.make_recovery_proof(
                command,
                observed_at=NOW + timedelta(seconds=13),
                complete=True,
                success=True,
            ),
            incident_resolution="contained",
        )
        self.assertEqual("terminal", terminal.state)
        self.assertEqual("contained", self.store.list_incidents("command-1")[0].state)
        self.assertGreater(self.store.get_reserved_exposure()[0], Decimal("0"))
        self.assertEqual("consumed", self.store.get_recovery_permit_state(permit.permit_id))

    def test_recovery_crash_after_prepare_becomes_unknown_without_retry(self) -> None:
        _, _, command = self.queue_recovery_fixture()
        claim = self.store.claim_next_recovery(
            "recovery-worker", at=NOW + timedelta(seconds=8), lease_seconds=5
        )
        assert claim is not None
        signing_authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        signed = self.make_signed_recovery(
            command,
            signing_authority_hash=signing_authority.authority_hash,
        )
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="recovery-attempt",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )
        self.store.require_recovery_submission_authority(
            command.recovery_command_id,
            attempt.attempt_id,
            signed.evidence_hash,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=9, milliseconds=1),
        )
        self.assertIsNone(
            self.store.claim_next_recovery(
                "replacement", at=NOW + timedelta(seconds=14), lease_seconds=5
            )
        )
        self.assertEqual(
            "submitted_unknown",
            self.store.get_recovery_command(command.recovery_command_id).state,
        )
        self.assertEqual(
            "unknown",
            self.store.get_recovery_attempt(command.recovery_command_id).state,
        )
        with self.assertRaises(StateConflict):
            self.store.prepare_recovery_attempt(
                command.recovery_command_id,
                "recovery-worker",
                claim.fencing_token,
                attempt_id="retry",
                signed_evidence=signed,
                at=NOW + timedelta(seconds=14),
            )

    def test_recovery_prepare_without_submission_authority_expires_proven_unsent(self) -> None:
        _, _, command = self.queue_recovery_fixture()
        claim = self.store.claim_next_recovery(
            "recovery-worker", at=NOW + timedelta(seconds=8), lease_seconds=5
        )
        assert claim is not None
        authority = self.store.require_recovery_signing_authority(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            at=NOW + timedelta(seconds=8, milliseconds=1),
        )
        signed = self.make_signed_recovery(
            command,
            signing_authority_hash=authority.authority_hash,
        )
        attempt = self.store.prepare_recovery_attempt(
            command.recovery_command_id,
            "recovery-worker",
            claim.fencing_token,
            attempt_id="recovery-never-authorized",
            signed_evidence=signed,
            at=NOW + timedelta(seconds=9),
        )

        self.assertIsNone(
            self.store.claim_next_recovery(
                "replacement", at=NOW + timedelta(seconds=14), lease_seconds=5
            )
        )
        self.assertEqual(
            "terminal",
            self.store.get_recovery_command(command.recovery_command_id).state,
        )
        self.assertEqual(
            "prepared",
            self.store.get_recovery_attempt(command.recovery_command_id).state,
        )
        self.assertEqual(
            attempt.attempt_id,
            self.store.get_recovery_outbox(command.recovery_command_id).current_attempt_id,
        )
        with self.assertRaises(RecordNotFound):
            self.store.get_recovery_transport_evidence(command.recovery_command_id)


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
        second_grant = make_infrastructure_grant(
            second_ticket,
            grant_id="infrastructure-grant-sol",
        )
        self.store.register_infrastructure_grant(second_grant, at=NOW)
        self.store.register_ticket(
            second_ticket,
            infrastructure_grant_hash=second_grant.grant_hash,
            stored_at=NOW + timedelta(milliseconds=1),
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
