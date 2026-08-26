from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from trading_harness.errors import StateConflict, StorageError, ValidationError
from trading_harness.execution_store import ExecutionStore
from trading_harness.executor_config import (
    ExecutorConfig,
    ExecutorConfigDrift,
    parse_executor_config,
)
from trading_harness.executor_runtime_store import (
    ExecutorRuntimeStore,
    ManualHaltReason,
    RuntimeLease,
    RuntimeLeaseState,
)
from trading_harness.executor_status import ExecutorProcessState, ExecutorRiskGate


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def config_text(root: Path, *, poll_interval_ms: int = 1000) -> str:
    for name in ("execution", "nonce", "daily-loss", "learning", "socket"):
        directory = root / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "runtime-node-secret-label"
executor_uid = {os.geteuid()}
research_uid = {os.geteuid() + 1}
control_uid = {os.geteuid() + 2}
account_id = "runtime-account-secret-label"
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
poll_interval_ms = {poll_interval_ms}
reconcile_interval_ms = 5000

[credential]
provider = "macos_keychain_generic_password"
service = "runtime-keychain-secret-service"
account = "runtime-keychain-secret-account"
timeout_seconds = 5

[approval_credential]
provider = "macos_keychain_generic_password"
service = "runtime-approval-secret-service"
account = "runtime-approval-secret-account"
timeout_seconds = 5

[recovery_credential]
provider = "macos_keychain_generic_password"
service = "runtime-recovery-secret-service"
account = "runtime-recovery-secret-account"
timeout_seconds = 5

[grant_credential]
provider = "macos_keychain_generic_password"
service = "runtime-grant-secret-service"
account = "runtime-grant-secret-account"
timeout_seconds = 5

[paths]
execution_database = "{root / 'execution' / 'execution.sqlite3'}"
nonce_database = "{root / 'nonce' / 'nonce.sqlite3'}"
daily_loss_database = "{root / 'daily-loss' / 'daily-loss.sqlite3'}"
learning_database = "{root / 'learning' / 'learning.sqlite3'}"
staging_database = "{root / 'learning' / 'staging.sqlite3'}"
control_socket = "{root / 'socket' / 'control.sock'}"
'''


class ExecutorRuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).absolute()
        self.config = parse_executor_config(config_text(self.root), environ={})
        self.clock = FakeClock(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc))
        self.store = ExecutorRuntimeStore(self.config, clock=self.clock)

    @staticmethod
    def _file_contents(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_existing_only_rejects_invalid_files_without_mutation(self) -> None:
        fixtures: list[tuple[str, Path, ExecutorConfig]] = []

        missing_root = self.root / "missing"
        missing_root.mkdir()
        missing_config = parse_executor_config(config_text(missing_root), environ={})
        fixtures.append(("missing", missing_root, missing_config))

        zero_root = self.root / "zero-byte"
        zero_root.mkdir()
        zero_config = parse_executor_config(config_text(zero_root), environ={})
        zero_path = zero_config.paths.execution_database
        zero_path.touch()
        fixtures.append(("zero-byte", zero_root, zero_config))

        empty_root = self.root / "schema-less"
        empty_root.mkdir()
        empty_config = parse_executor_config(config_text(empty_root), environ={})
        empty_path = empty_config.paths.execution_database
        connection = sqlite3.connect(empty_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("VACUUM")
        finally:
            connection.close()
        fixtures.append(("schema-less", empty_root, empty_config))

        wrong_root = self.root / "wrong-store"
        wrong_root.mkdir()
        wrong_config = parse_executor_config(config_text(wrong_root), environ={})
        ExecutionStore(
            wrong_config.paths.execution_database,
            environment=wrong_config.environment,
            account_id=wrong_config.account_id,
            max_reserved_loss=wrong_config.max_reserved_loss,
            max_reserved_notional=wrong_config.max_reserved_notional,
        )
        fixtures.append(("wrong-store", wrong_root, wrong_config))

        symlink_root = self.root / "symlink"
        symlink_root.mkdir()
        symlink_config = parse_executor_config(config_text(symlink_root), environ={})
        symlink_config.paths.execution_database.symlink_to(
            self.config.paths.execution_database
        )
        fixtures.append(("symlink", symlink_root, symlink_config))

        hardlink_root = self.root / "hardlink"
        hardlink_root.mkdir()
        hardlink_config = parse_executor_config(config_text(hardlink_root), environ={})
        os.link(
            self.config.paths.execution_database,
            hardlink_config.paths.execution_database,
        )
        fixtures.append(("hardlink", hardlink_root, hardlink_config))

        for name, root, config in fixtures:
            with self.subTest(name=name):
                before = self._file_contents(root)
                with self.assertRaises((StorageError, ValidationError)):
                    ExecutorRuntimeStore(config, clock=self.clock, must_exist=True)
                self.assertEqual(before, self._file_contents(root))

    def test_existing_only_valid_reopen_is_read_only_during_verification(self) -> None:
        before = self._file_contents(self.root)
        reopened = ExecutorRuntimeStore(
            self.config, clock=self.clock, must_exist=True
        )
        self.assertEqual(before, self._file_contents(self.root))
        self.assertEqual(RuntimeLeaseState.NOT_STARTED, reopened.read().lease_state)

    def test_existing_only_rejects_extra_runtime_trigger_without_mutation(self) -> None:
        with closing(
            sqlite3.connect(self.config.paths.execution_database)
        ) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER stealth_runtime_trigger
                BEFORE INSERT ON executor_runtime_events
                BEGIN SELECT RAISE(IGNORE); END
                """
            )
        before = self._file_contents(self.root)

        with self.assertRaisesRegex(StorageError, "schema does not match"):
            ExecutorRuntimeStore(
                self.config,
                clock=self.clock,
                must_exist=True,
            )

        self.assertEqual(before, self._file_contents(self.root))

    def test_existing_only_verifies_committed_state_retained_in_wal(self) -> None:
        reader = sqlite3.connect(self.config.paths.execution_database)
        try:
            reader.execute("BEGIN")
            reader.execute(
                "SELECT COUNT(*) FROM executor_runtime_events"
            ).fetchone()
            self.store.acquire(instance_id="wal-worker", lease_seconds=30)
            wal_path = Path(f"{self.config.paths.execution_database}-wal")
            self.assertGreater(wal_path.stat().st_size, 0)
            before = self._file_contents(self.root)
            reopened = ExecutorRuntimeStore(
                self.config, clock=self.clock, must_exist=True
            )
            self.assertEqual(before, self._file_contents(self.root))
        finally:
            reader.close()
        self.assertEqual(RuntimeLeaseState.ACTIVE, reopened.read().lease_state)

    def test_initial_read_is_redacted_not_started_and_halted(self) -> None:
        status = self.store.read()
        self.assertEqual(status.lease_state, RuntimeLeaseState.NOT_STARTED)
        self.assertEqual(status.process_state, ExecutorProcessState.STOPPED)
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.assertFalse(status.lease_current)
        self.assertFalse(status.heartbeat_current)
        self.assertEqual(status.fencing_token, 0)
        self.assertTrue(self.store.verify_journal())

    def test_acquire_begins_halted_and_ready_requires_heartbeat(self) -> None:
        lease = self.store.acquire(instance_id="worker-one", lease_seconds=30)
        self.assertEqual(lease.fencing_token, 1)
        starting = self.store.read()
        self.assertEqual(starting.process_state, ExecutorProcessState.STARTING)
        self.assertEqual(starting.declared_risk_gate, ExecutorRiskGate.HALTED)
        self.assertEqual(starting.effective_risk_gate, ExecutorRiskGate.HALTED)

        self.store.transition(
            instance_id="worker-one",
            fencing_token=lease.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        with self.assertRaisesRegex(StateConflict, "heartbeat"):
            self.store.transition(
                instance_id="worker-one",
                fencing_token=lease.fencing_token,
                risk_gate="ready",
            )

    def test_can_share_execution_database_without_cross_table_access(self) -> None:
        execution = ExecutionStore(
            self.config.paths.execution_database,
            environment=self.config.environment,
            account_id=self.config.account_id,
            max_reserved_loss=self.config.max_reserved_loss,
            max_reserved_notional=self.config.max_reserved_notional,
        )
        runtime = ExecutorRuntimeStore(self.config, clock=self.clock)
        runtime.acquire(instance_id="shared-worker", lease_seconds=30)

        self.assertEqual(execution.list_commands(), ())
        self.assertEqual(execution.list_recovery_commands(), ())
        self.assertEqual(runtime.read().fencing_token, 1)
        connection = sqlite3.connect(self.config.paths.execution_database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("execution_commands", tables)
        self.assertIn("executor_runtime_state", tables)

    def test_full_lifecycle_and_manual_halt_are_fenced(self) -> None:
        lease = self.store.acquire(instance_id="worker-one", lease_seconds=30)
        self.store.heartbeat(
            instance_id="worker-one", fencing_token=lease.fencing_token, lease_seconds=30
        )
        self.store.transition(
            instance_id="worker-one",
            fencing_token=lease.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        ready_lease = self.store.transition(
            instance_id="worker-one",
            fencing_token=lease.fencing_token,
            risk_gate="ready",
        )
        ready = self.store.read()
        self.assertEqual(ready.effective_risk_gate, ExecutorRiskGate.READY)
        self.assertTrue(ready.heartbeat_current)

        halted = self.store.engage_manual_halt(reason=ManualHaltReason.OPERATOR)
        self.assertTrue(halted.manual_halt)
        self.assertEqual(halted.manual_halt_reason, ManualHaltReason.OPERATOR)
        self.assertEqual(halted.effective_risk_gate, ExecutorRiskGate.HALTED)
        with self.assertRaises(StateConflict):
            self.store.transition(
                instance_id="worker-one",
                fencing_token=lease.fencing_token,
                risk_gate="reconciling",
            )
        with self.assertRaises(StateConflict):
            self.store.clear_manual_halt(
                instance_id="worker-one",
                fencing_token=lease.fencing_token,
                expected_revision=ready_lease.revision,
            )

        current = self.store.read()
        self.store.clear_manual_halt(
            instance_id="worker-one",
            fencing_token=lease.fencing_token,
            expected_revision=current.revision,
        )
        cleared = self.store.read()
        self.assertFalse(cleared.manual_halt)
        self.assertEqual(cleared.declared_risk_gate, ExecutorRiskGate.HALTED)

        self.store.transition(
            instance_id="worker-one",
            fencing_token=lease.fencing_token,
            risk_gate="reconciling",
        )
        with self.assertRaisesRegex(StateConflict, "stopped before"):
            self.store.release(
                instance_id="worker-one", fencing_token=lease.fencing_token
            )
        self.store.request_stop(
            instance_id="worker-one", fencing_token=lease.fencing_token
        )
        stopping = self.store.read()
        self.assertEqual(stopping.process_state, ExecutorProcessState.STOPPING)
        self.assertEqual(stopping.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.store.mark_stopped(
            instance_id="worker-one", fencing_token=lease.fencing_token
        )
        released_lease = self.store.release(
            instance_id="worker-one", fencing_token=lease.fencing_token
        )
        released = self.store.read()
        self.assertEqual(released.lease_state, RuntimeLeaseState.RELEASED)
        self.assertFalse(released.lease_current)
        self.assertEqual(released_lease.fencing_token, 1)
        self.assertTrue(self.store.verify_journal())

        restarted = self.store.acquire(instance_id="worker-two", lease_seconds=30)
        self.assertEqual(restarted.fencing_token, 2)
        self.assertEqual(self.store.read().process_state, ExecutorProcessState.STARTING)

    def test_ready_requires_current_heartbeat(self) -> None:
        lease = self.store.acquire(instance_id="heartbeat-worker", lease_seconds=10)
        self.store.transition(
            instance_id="heartbeat-worker",
            fencing_token=lease.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        with self.assertRaisesRegex(StateConflict, "heartbeat"):
            self.store.transition(
                instance_id="heartbeat-worker",
                fencing_token=lease.fencing_token,
                risk_gate="ready",
            )
        self.store.heartbeat(
            instance_id="heartbeat-worker",
            fencing_token=lease.fencing_token,
            lease_seconds=10,
        )
        self.store.transition(
            instance_id="heartbeat-worker",
            fencing_token=lease.fencing_token,
            risk_gate="ready",
        )
        self.assertEqual(self.store.read().effective_risk_gate, ExecutorRiskGate.READY)
        self.clock.now += timedelta(seconds=10)
        expired = self.store.read()
        self.assertEqual(expired.declared_risk_gate, ExecutorRiskGate.READY)
        self.assertEqual(expired.effective_risk_gate, ExecutorRiskGate.HALTED)
        self.assertFalse(expired.lease_current)
        self.assertFalse(expired.heartbeat_current)

    def test_backward_clock_read_fails_effective_gate_closed(self) -> None:
        lease = self.store.acquire(instance_id="clock-worker", lease_seconds=30)
        self.store.heartbeat(
            instance_id="clock-worker",
            fencing_token=lease.fencing_token,
            lease_seconds=30,
        )
        self.store.transition(
            instance_id="clock-worker",
            fencing_token=lease.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        self.store.transition(
            instance_id="clock-worker",
            fencing_token=lease.fencing_token,
            risk_gate="ready",
        )
        self.clock.now -= timedelta(microseconds=1)
        rolled_back = self.store.read()
        self.assertFalse(rolled_back.lease_current)
        self.assertFalse(rolled_back.heartbeat_current)
        self.assertEqual(rolled_back.effective_risk_gate, ExecutorRiskGate.HALTED)

    def test_concurrent_singleton_acquire_and_expiry_fence_stale_worker(self) -> None:
        peer = ExecutorRuntimeStore(self.config, clock=self.clock)
        barrier = threading.Barrier(2)
        results: list[object] = []

        def acquire(store: ExecutorRuntimeStore, identity: str) -> None:
            barrier.wait()
            try:
                results.append(store.acquire(instance_id=identity, lease_seconds=5))
            except Exception as error:
                results.append(error)

        threads = (
            threading.Thread(target=acquire, args=(self.store, "worker-a")),
            threading.Thread(target=acquire, args=(peer, "worker-b")),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        leases = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(leases), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], StateConflict)
        first = leases[0]
        self.assertIsInstance(first, RuntimeLease)
        first_token = first.fencing_token
        self.clock.now += timedelta(seconds=5)
        replacement = peer.acquire(instance_id="replacement", lease_seconds=5)
        self.assertEqual(replacement.fencing_token, 2)
        for stale_identity in ("worker-a", "worker-b"):
            with self.assertRaises(StateConflict):
                self.store.heartbeat(
                    instance_id=stale_identity,
                    fencing_token=first_token,
                    lease_seconds=5,
                )

    def test_manual_halt_survives_expired_takeover(self) -> None:
        lease = self.store.acquire(instance_id="old-worker", lease_seconds=5)
        self.store.engage_manual_halt(reason="daily_loss")
        self.clock.now += timedelta(seconds=5)
        replacement = self.store.acquire(instance_id="new-worker", lease_seconds=5)
        status = self.store.read()
        self.assertEqual(replacement.fencing_token, 2)
        self.assertTrue(status.manual_halt)
        self.assertEqual(status.manual_halt_reason, ManualHaltReason.DAILY_LOSS)
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)
        with self.assertRaises(StateConflict):
            self.store.clear_manual_halt(
                instance_id="old-worker",
                fencing_token=lease.fencing_token,
                expected_revision=status.revision,
            )

    def test_attended_acknowledgement_requires_exact_stale_halt_and_stays_halted(self) -> None:
        self.store.acquire(instance_id="failed-worker", lease_seconds=5)
        halted = self.store.engage_manual_halt(
            reason=ManualHaltReason.INTERNAL_ERROR
        )
        with self.assertRaisesRegex(StateConflict, "live executor"):
            self.store.acknowledge_stale_manual_halt(
                expected_revision=halted.revision,
                expected_reason=ManualHaltReason.INTERNAL_ERROR,
            )
        self.clock.now += timedelta(seconds=5)
        with self.assertRaisesRegex(StateConflict, "revision"):
            self.store.acknowledge_stale_manual_halt(
                expected_revision=halted.revision - 1,
                expected_reason=ManualHaltReason.INTERNAL_ERROR,
            )
        with self.assertRaisesRegex(StateConflict, "reason"):
            self.store.acknowledge_stale_manual_halt(
                expected_revision=halted.revision,
                expected_reason=ManualHaltReason.OPERATOR,
            )

        acknowledged = self.store.acknowledge_stale_manual_halt(
            expected_revision=halted.revision,
            expected_reason=ManualHaltReason.INTERNAL_ERROR,
        )

        self.assertFalse(acknowledged.manual_halt)
        self.assertEqual(
            ExecutorRiskGate.HALTED, acknowledged.declared_risk_gate
        )
        self.assertEqual(
            ExecutorRiskGate.HALTED, acknowledged.effective_risk_gate
        )
        self.assertTrue(self.store.verify_journal())

    def test_config_drift_is_rejected_on_reopen(self) -> None:
        ExecutorRuntimeStore(self.config, clock=self.clock)
        drifted = parse_executor_config(
            config_text(self.root, poll_interval_ms=1001), environ={}
        )
        with self.assertRaises(ExecutorConfigDrift):
            ExecutorRuntimeStore(drifted, clock=self.clock)

    def test_restart_reconstructs_exact_active_state_and_preserves_fence(self) -> None:
        lease = self.store.acquire(instance_id="restart-worker", lease_seconds=30)
        self.store.heartbeat(
            instance_id="restart-worker",
            fencing_token=lease.fencing_token,
            lease_seconds=30,
        )
        self.store.transition(
            instance_id="restart-worker",
            fencing_token=lease.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        before = self.store.read()

        reopened = ExecutorRuntimeStore(self.config, clock=self.clock)
        after = reopened.read()
        self.assertEqual(after.state_hash, before.state_hash)
        self.assertEqual(after.journal_chain_hash, before.journal_chain_hash)
        self.assertEqual(after.fencing_token, before.fencing_token)
        self.assertEqual(after.revision, before.revision)
        with self.assertRaisesRegex(StateConflict, "another executor"):
            reopened.acquire(instance_id="competing-worker", lease_seconds=30)

    def test_status_and_database_do_not_store_raw_runtime_identity(self) -> None:
        secret_instance = "worker-raw-secret-identity"
        self.store.acquire(instance_id=secret_instance, lease_seconds=30)
        status = self.store.read()
        encoded = json.dumps(status.as_dict(), sort_keys=True)
        represented = repr(status)
        for secret in (
            secret_instance,
            self.config.node_id,
            self.config.account_id,
            self.config.main_account_address,
            self.config.api_wallet_address,
            self.config.credential.service,
            self.config.credential.account,
            str(self.config.paths.execution_database),
        ):
            self.assertNotIn(secret, encoded)
            self.assertNotIn(secret, represented)

        connection = sqlite3.connect(self.config.paths.execution_database)
        try:
            runtime_tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name LIKE 'executor_runtime_%'
                    """
                )
            }
            self.assertEqual(
                runtime_tables,
                {
                    "executor_runtime_schema_migrations",
                    "executor_runtime_deployment",
                    "executor_runtime_state",
                    "executor_runtime_events",
                },
            )
            raw_values = " ".join(
                str(value)
                for table in runtime_tables
                for row in connection.execute(f"SELECT * FROM {table}")
                for value in row
            )
        finally:
            connection.close()
        self.assertNotIn(secret_instance, raw_values)
        self.assertNotIn(self.config.account_id, raw_values)

    def test_state_and_journal_tampering_fail_closed(self) -> None:
        self.store.acquire(instance_id="tamper-worker", lease_seconds=30)
        connection = sqlite3.connect(self.config.paths.execution_database)
        try:
            connection.execute(
                "UPDATE executor_runtime_state SET risk_gate = 'ready' WHERE singleton = 1"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            self.store.read()

        other_root = self.root / "journal"
        other_root.mkdir()
        other_config = parse_executor_config(config_text(other_root), environ={})
        other = ExecutorRuntimeStore(other_config, clock=self.clock)
        other.acquire(instance_id="journal-worker", lease_seconds=30)
        connection = sqlite3.connect(other_config.paths.execution_database)
        try:
            connection.execute("DROP TRIGGER executor_runtime_events_no_update")
            connection.execute(
                "UPDATE executor_runtime_events SET payload_json = '{}' WHERE event_sequence = 1"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError):
            other.read()


if __name__ == "__main__":
    unittest.main()
