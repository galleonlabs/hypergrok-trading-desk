from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from trading_harness.daily_loss import DailyLossBinding, DailyLossLedger
from trading_harness.executor_config import ExecutorConfig, parse_executor_config
from trading_harness.executor_status import (
    ExecutorBlocker,
    ExecutorProcessState,
    ExecutorRiskGate,
    RedactedExecutorConfigStatus,
    build_executor_status,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def config_text(root: Path) -> str:
    for name in ("execution", "nonce", "daily-loss", "learning", "socket"):
        directory = root / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "sensitive-node-label"
executor_uid = {os.geteuid()}
research_uid = {os.geteuid() + 1}
control_uid = {os.geteuid() + 2}
account_id = "sensitive-account-label"
main_account_address = "0x1111111111111111111111111111111111111111"
api_wallet_address = "0x2222222222222222222222222222222222222222"
daily_loss_limit = "25"
max_reserved_loss = "5"
max_reserved_notional = "100"
max_leverage = "2"
risk_policy_hash = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
allowed_instruments = ["ETH-PERP"]
allowed_asset_ids = [1]
recovery_cloids = ["0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]
settlement_currency = "USDC"
poll_interval_ms = 1000
reconcile_interval_ms = 5000

[credential]
provider = "macos_keychain_generic_password"
service = "sensitive-keychain-service"
account = "sensitive-keychain-account"
timeout_seconds = 5

[approval_credential]
provider = "macos_keychain_generic_password"
service = "sensitive-approval-service"
account = "sensitive-approval-account"
timeout_seconds = 5

[recovery_credential]
provider = "macos_keychain_generic_password"
service = "sensitive-recovery-service"
account = "sensitive-recovery-account"
timeout_seconds = 5

[grant_credential]
provider = "macos_keychain_generic_password"
service = "sensitive-grant-service"
account = "sensitive-grant-account"
timeout_seconds = 5

[paths]
execution_database = "{root / 'execution' / 'sensitive-execution.sqlite3'}"
nonce_database = "{root / 'nonce' / 'sensitive-nonce.sqlite3'}"
daily_loss_database = "{root / 'daily-loss' / 'sensitive-daily-loss.sqlite3'}"
learning_database = "{root / 'learning' / 'sensitive-learning.sqlite3'}"
staging_database = "{root / 'learning' / 'sensitive-staging.sqlite3'}"
control_socket = "{root / 'socket' / 'sensitive-control.sock'}"
'''


class ExecutorStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).absolute()
        self.config: ExecutorConfig = parse_executor_config(
            config_text(self.root), environ={}
        )
        self.now = datetime(2026, 8, 25, 16, 30, tzinfo=timezone.utc)
        binding = DailyLossBinding(
            account_id=self.config.account_id,
            environment=self.config.environment,
            config_hash=self.config.config_hash,
            daily_loss_limit=self.config.daily_loss_limit,
            settlement_currency=self.config.settlement_currency,
        )
        self.ledger = DailyLossLedger(
            self.root / "loss.sqlite3", binding=binding, clock=lambda: self.now
        )
        start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        for source in ("fills", "funding"):
            self.ledger.record_coverage(
                coverage_id=f"{source}-coverage",
                source=source,
                covered_from=start,
                covered_through=self.now,
                source_cursor_hash=digest(source),
            )
        self.loss = self.ledger.snapshot()

    def test_ready_status_is_structured_hashed_and_redacted(self) -> None:
        status = build_executor_status(
            config=self.config,
            process_state=ExecutorProcessState.RUNNING,
            declared_risk_gate=ExecutorRiskGate.READY,
            started_at=self.now.replace(hour=16),
            observed_at=self.now,
            heartbeat_at=self.now,
            credential_loaded=True,
            account_reconciled=True,
            config_matches_durable_state=True,
            daily_loss=self.loss,
        )
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.READY)
        self.assertEqual(status.blockers, ())
        self.assertRegex(status.status_hash, r"^[0-9a-f]{64}$")

        encoded = json.dumps(status.as_dict(), sort_keys=True)
        represented = repr(status)
        sensitive = (
            "sensitive-node-label",
            "sensitive-account-label",
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
            "sensitive-keychain-service",
            "sensitive-keychain-account",
            "sensitive-approval-service",
            "sensitive-approval-account",
            "sensitive-recovery-service",
            "sensitive-recovery-account",
            "sensitive-grant-service",
            "sensitive-grant-account",
            str(self.root),
            "sensitive-execution.sqlite3",
            "sensitive-learning.sqlite3",
            "sensitive-control.sock",
        )
        for value in sensitive:
            with self.subTest(value=value):
                self.assertNotIn(value, encoded)
                self.assertNotIn(value, represented)

    def test_config_projection_contains_only_fingerprints_not_raw_metadata(self) -> None:
        projected = RedactedExecutorConfigStatus.from_config(self.config)
        values = projected.as_dict()
        fingerprint_fields = (
            "node_fingerprint",
            "account_fingerprint",
            "main_account_fingerprint",
            "api_wallet_fingerprint",
            "credential_service_fingerprint",
            "credential_account_fingerprint",
            "approval_service_fingerprint",
            "approval_account_fingerprint",
            "recovery_service_fingerprint",
            "recovery_account_fingerprint",
            "grant_service_fingerprint",
            "grant_account_fingerprint",
            "recovery_cloids_fingerprint",
            "execution_database_fingerprint",
            "nonce_database_fingerprint",
            "daily_loss_database_fingerprint",
            "learning_database_fingerprint",
            "staging_database_fingerprint",
            "control_socket_fingerprint",
        )
        for field in fingerprint_fields:
            self.assertRegex(values[field], r"^[0-9a-f]{64}$")
        self.assertNotEqual(values["main_account_fingerprint"], values["api_wallet_fingerprint"])

    def test_incomplete_or_drifted_observations_force_effective_halt(self) -> None:
        status = build_executor_status(
            config=self.config,
            process_state="running",
            declared_risk_gate="ready",
            started_at=self.now.replace(hour=16),
            observed_at=self.now,
            heartbeat_at=None,
            credential_loaded=False,
            account_reconciled=False,
            config_matches_durable_state=False,
            daily_loss=None,
            manual_halt=True,
        )
        self.assertEqual(status.declared_risk_gate, ExecutorRiskGate.READY)
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.assertEqual(
            set(status.blockers),
            {
                ExecutorBlocker.CONFIG_DRIFT,
                ExecutorBlocker.CREDENTIAL_UNAVAILABLE,
                ExecutorBlocker.ACCOUNT_RECONCILIATION_INCOMPLETE,
                ExecutorBlocker.DAILY_LOSS_COVERAGE_INCOMPLETE,
                ExecutorBlocker.HEARTBEAT_UNAVAILABLE,
                ExecutorBlocker.MANUAL_HALT,
            },
        )

    def test_daily_loss_limit_reached_forces_halt(self) -> None:
        self.ledger.record_realized_pnl(
            event_id="limit-loss",
            source_ref="limit-fill",
            occurred_at=self.now,
            realized_pnl="-25",
        )
        exhausted = self.ledger.snapshot()
        status = build_executor_status(
            config=self.config,
            process_state="running",
            declared_risk_gate="ready",
            started_at=self.now.replace(hour=16),
            observed_at=self.now,
            heartbeat_at=self.now,
            credential_loaded=True,
            account_reconciled=True,
            config_matches_durable_state=True,
            daily_loss=exhausted,
        )
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.assertIn(ExecutorBlocker.DAILY_LOSS_LIMIT_REACHED, status.blockers)

    def test_stale_heartbeat_and_loss_snapshot_force_halt(self) -> None:
        observed = self.now + timedelta(seconds=11)
        status = build_executor_status(
            config=self.config,
            process_state="running",
            declared_risk_gate="ready",
            started_at=self.now.replace(hour=16),
            observed_at=observed,
            heartbeat_at=self.now,
            credential_loaded=True,
            account_reconciled=True,
            config_matches_durable_state=True,
            daily_loss=self.loss,
        )
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.assertIn(ExecutorBlocker.HEARTBEAT_STALE, status.blockers)
        self.assertIn(ExecutorBlocker.DAILY_LOSS_SNAPSHOT_STALE, status.blockers)

    def test_wrong_daily_loss_binding_is_rejected(self) -> None:
        changed = parse_executor_config(
            config_text(self.root).replace('daily_loss_limit = "25"', 'daily_loss_limit = "24"'),
            environ={},
        )
        with self.assertRaisesRegex(ValueError, "binding does not match"):
            build_executor_status(
                config=changed,
                process_state="running",
                declared_risk_gate="ready",
                started_at=self.now.replace(hour=16),
                observed_at=self.now,
                heartbeat_at=self.now,
                credential_loaded=True,
                account_reconciled=True,
                config_matches_durable_state=True,
                daily_loss=self.loss,
            )


if __name__ == "__main__":
    unittest.main()
