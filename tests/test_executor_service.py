from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.errors import StorageError, ValidationError
from trading_harness.daily_loss import DailyLossLedger
from trading_harness.execution_store import ExecutionStore
from trading_harness.executor_config import parse_executor_config
from trading_harness.executor_runtime import RuntimeStep
from trading_harness.execution_learning_sync import LearningProjectionError
from trading_harness.executor_state_binding import MAX_SHARED_STATE_FILE_BYTES
from trading_harness.executor_runtime_store import ExecutorRuntimeStore
from trading_harness.executor_service import (
    _validate_state_layout,
    _verify_state_database_binding,
    _wallet_address,
    build_active_testnet_executor_service,
    initialize_testnet_executor_state,
    open_testnet_executor_state,
)
from trading_harness.hyperliquid_account import fetch_account_snapshot
from trading_harness.hyperliquid_loss_sync import HyperliquidLossSyncError
from trading_harness.learning_ledger import LearningLedger, LedgerIntegrityError
from trading_harness.nonce import PersistentNonceAllocator
from trading_harness.planning import RiskSizingPolicy
from trading_harness.staging_inbox import TradeStagingInbox
from tests.test_account_risk import flat_clearing
from tests.test_hyperliquid_account import ACCOUNT, FixtureTransport
from tests.test_learning_quote_service import config_text
from tests.test_node import AT


class FakeWallet:
    def __init__(self, address: str) -> None:
        self.address = address


class EmptyLossTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, payload):
        self.calls.append((endpoint, dict(payload)))
        if self.fail:
            raise OSError("offline")
        if payload["type"] in {"userFillsByTime", "userFunding"}:
            return []
        raise AssertionError("unexpected info read")


class ExecutorServiceCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).absolute()
        self.policy = RiskSizingPolicy(
            version="service-test-v1",
            entry_slippage_bps=Decimal("0"),
            exit_slippage_bps=Decimal("0"),
            stop_gap_bps=Decimal("0"),
            round_trip_fee_bps=Decimal("0"),
        )
        self.config = parse_executor_config(
            config_text(self.root, self.policy.policy_hash), environ={}
        )
        clearing = flat_clearing()
        clearing["time"] = int((AT - timedelta(milliseconds=500)).timestamp() * 1000)
        self.snapshot = fetch_account_snapshot(
            ACCOUNT,
            "testnet",
            transport=FixtureTransport(clearing=clearing, orders=[]),
            clock=lambda: AT,
        )

    def _main_state_files(self) -> tuple[Path, ...]:
        return (
            self.config.paths.execution_database,
            self.config.paths.nonce_database,
            self.config.paths.daily_loss_database,
            self.config.paths.learning_database,
            self.config.paths.staging_database,
        )

    @staticmethod
    def _sidecar(database: Path, suffix: str) -> Path:
        return Path(str(database) + suffix)

    def _materialize_layout(self, *sidecars: Path) -> None:
        for path in (*self._main_state_files(), *sidecars):
            path.write_bytes(b"state")
            path.chmod(0o600)

    @staticmethod
    def _lstat_patch(
        overrides: dict[Path, dict[str, int]],
    ):
        real_lstat = Path.lstat

        def selected_lstat(path: Path):
            metadata = real_lstat(path)
            selected = overrides.get(Path(path))
            if selected is None:
                return metadata
            return SimpleNamespace(
                st_mode=selected.get("st_mode", metadata.st_mode),
                st_nlink=selected.get("st_nlink", metadata.st_nlink),
                st_size=selected.get("st_size", metadata.st_size),
                st_uid=selected.get("st_uid", metadata.st_uid),
            )

        return patch.object(Path, "lstat", autospec=True, side_effect=selected_lstat)

    def test_config_bound_multi_uid_sidecars_are_accepted(self) -> None:
        execution_wal = self._sidecar(
            self.config.paths.execution_database, "-wal"
        )
        learning_wal = self._sidecar(
            self.config.paths.learning_database, "-wal"
        )
        learning_shm = self._sidecar(
            self.config.paths.learning_database, "-shm"
        )
        staging_wal = self._sidecar(
            self.config.paths.staging_database, "-wal"
        )
        staging_shm = self._sidecar(
            self.config.paths.staging_database, "-shm"
        )
        self._materialize_layout(
            execution_wal,
            learning_wal,
            learning_shm,
            staging_wal,
            staging_shm,
        )
        overrides = {
            execution_wal: {"st_uid": self.config.control_uid},
            learning_wal: {"st_uid": self.config.research_uid},
            learning_shm: {"st_uid": self.config.control_uid},
            staging_wal: {"st_uid": self.config.control_uid},
            staging_shm: {"st_uid": self.config.research_uid},
        }

        with self._lstat_patch(overrides):
            _validate_state_layout(self.config, existing=True)

    def test_every_main_database_must_remain_executor_owned(self) -> None:
        self._materialize_layout()

        for database in self._main_state_files():
            with self.subTest(database=database.name):
                with (
                    self._lstat_patch(
                        {database: {"st_uid": self.config.control_uid}}
                    ),
                    self.assertRaisesRegex(ValidationError, "invalid owner"),
                ):
                    _validate_state_layout(self.config, existing=True)

    def test_research_owned_execution_sidecar_is_rejected(self) -> None:
        execution_wal = self._sidecar(
            self.config.paths.execution_database, "-wal"
        )
        self._materialize_layout(execution_wal)

        with (
            self._lstat_patch(
                {execution_wal: {"st_uid": self.config.research_uid}}
            ),
            self.assertRaisesRegex(ValidationError, "invalid owner"),
        ):
            _validate_state_layout(self.config, existing=True)

    def test_non_executor_nonce_and_daily_loss_sidecars_are_rejected(self) -> None:
        cases = (
            (
                self._sidecar(self.config.paths.nonce_database, "-wal"),
                self.config.control_uid,
            ),
            (
                self._sidecar(self.config.paths.daily_loss_database, "-shm"),
                self.config.research_uid,
            ),
            (
                self._sidecar(self.config.paths.nonce_database, "-journal"),
                max(
                    self.config.executor_uid,
                    self.config.research_uid,
                    self.config.control_uid,
                )
                + 1000,
            ),
            (
                self._sidecar(self.config.paths.daily_loss_database, "-journal"),
                0,
            ),
        )
        self._materialize_layout(*(path for path, _ in cases))

        for sidecar, owner in cases:
            with self.subTest(sidecar=sidecar.name, owner=owner):
                with (
                    self._lstat_patch({sidecar: {"st_uid": owner}}),
                    self.assertRaisesRegex(ValidationError, "invalid owner"),
                ):
                    _validate_state_layout(self.config, existing=True)

    def test_orphan_sidecar_is_rejected_before_initialization(self) -> None:
        orphan = self._sidecar(self.config.paths.execution_database, "-wal")
        orphan.touch(mode=0o600)
        orphan.chmod(0o600)

        with self.assertRaisesRegex(ValidationError, "no main database"):
            _validate_state_layout(self.config, existing=False)

    def test_initialization_requires_an_all_empty_state_layout(self) -> None:
        existing = self.config.paths.execution_database
        existing.write_bytes(b"preexisting-state")
        existing.chmod(0o600)

        with self.assertRaisesRegex(ValidationError, "requires empty"):
            initialize_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertEqual(b"preexisting-state", existing.read_bytes())
        for path in self._main_state_files()[1:]:
            self.assertFalse(path.exists())

    def test_reinitialization_is_rejected_without_mutating_state(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        before = {path: path.read_bytes() for path in self._main_state_files()}

        with self.assertRaisesRegex(ValidationError, "requires empty"):
            initialize_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertEqual(
            before,
            {path: path.read_bytes() for path in self._main_state_files()},
        )

    def test_existing_empty_main_database_is_never_rebuilt(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        execution = self.config.paths.execution_database
        execution.write_bytes(b"")
        execution.chmod(0o600)

        with self.assertRaisesRegex(ValidationError, "main database is empty"):
            open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertEqual(b"", execution.read_bytes())

    def test_stale_private_verification_directory_requires_review(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        stale = (
            self.config.paths.execution_database.parent
            / ".trading-sqlite-verify-crash"
        )
        stale.mkdir(mode=0o700)

        with self.assertRaisesRegex(ValidationError, "requires review"):
            open_testnet_executor_state(self.config, clock=lambda: AT)

    def test_every_state_database_is_bound_to_exact_v2_config(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        before = {path: path.read_bytes() for path in self._main_state_files()}
        for path in self._main_state_files():
            _verify_state_database_binding(self.config, path)

        changed = parse_executor_config(
            config_text(self.root, self.policy.policy_hash).replace(
                "poll_interval_ms = 1000",
                "poll_interval_ms = 1001",
            ),
            environ={},
        )
        for path in self._main_state_files():
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValidationError, "does not match config"):
                    _verify_state_database_binding(changed, path)

        self.assertEqual(
            before,
            {path: path.read_bytes() for path in self._main_state_files()},
        )

    def test_state_layout_requires_configured_executor_process_uid(self) -> None:
        with (
            patch(
                "trading_harness.executor_service.os.geteuid",
                return_value=self.config.control_uid,
            ),
            self.assertRaisesRegex(ValidationError, "configured executor UID"),
        ):
            _validate_state_layout(self.config, existing=False)

    def test_state_artifacts_must_not_have_hardlinks(self) -> None:
        self._materialize_layout()
        linked = self.root / "execution-hardlink.sqlite3"
        os.link(self.config.paths.execution_database, linked)

        with self.assertRaisesRegex(ValidationError, "regular non-symlink"):
            _validate_state_layout(self.config, existing=True)

    def test_state_directories_and_files_require_exact_modes(self) -> None:
        self._materialize_layout()
        execution = self.config.paths.execution_database
        execution.chmod(0o400)
        with self.assertRaisesRegex(ValidationError, "mode 0600"):
            _validate_state_layout(self.config, existing=True)
        execution.chmod(0o600)

        execution.parent.chmod(0o500)
        try:
            with self.assertRaisesRegex(ValidationError, "mode 0700"):
                _validate_state_layout(self.config, existing=True)
        finally:
            execution.parent.chmod(0o700)

    def test_reopen_never_chmods_existing_state_artifacts(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)

        with patch.object(
            Path,
            "chmod",
            autospec=True,
            side_effect=AssertionError("reopen must not chmod existing artifacts"),
        ) as chmod:
            reopened = open_testnet_executor_state(self.config, clock=lambda: AT)

        chmod.assert_not_called()
        self.assertEqual(
            self.config.config_hash,
            reopened.runtime_store.read().config_hash,
        )

    def test_reopen_uses_existing_only_mode_for_every_store(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)

        with (
            patch(
                "trading_harness.executor_service.ExecutionStore",
                side_effect=ExecutionStore,
            ) as execution_store,
            patch(
                "trading_harness.executor_service.ExecutorRuntimeStore",
                side_effect=ExecutorRuntimeStore,
            ) as runtime_store,
            patch(
                "trading_harness.executor_service.DailyLossLedger",
                side_effect=DailyLossLedger,
            ) as daily_loss,
            patch(
                "trading_harness.executor_service.PersistentNonceAllocator",
                side_effect=PersistentNonceAllocator,
            ) as nonce,
            patch(
                "trading_harness.executor_service.LearningLedger",
                side_effect=LearningLedger,
            ) as learning,
            patch(
                "trading_harness.executor_service.TradeStagingInbox",
                side_effect=TradeStagingInbox,
            ) as staging,
        ):
            open_testnet_executor_state(self.config, clock=lambda: AT)

        for constructor in (
            execution_store,
            runtime_store,
            daily_loss,
            nonce,
            learning,
            staging,
        ):
            self.assertIs(constructor.call_args.kwargs["must_exist"], True)

    def test_deletion_after_validation_cannot_recreate_main_database(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        execution_database = self.config.paths.execution_database
        validate_layout = _validate_state_layout

        def validate_then_delete(
            config,
            *,
            existing: bool,
            include_shared: bool = True,
        ) -> None:
            validate_layout(
                config,
                existing=existing,
                include_shared=include_shared,
            )
            execution_database.unlink()

        with (
            patch(
                "trading_harness.executor_service._validate_state_layout",
                side_effect=validate_then_delete,
            ),
            self.assertRaises(ValidationError),
        ):
            open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertFalse(execution_database.exists())

    def test_wrong_store_swap_is_rejected_before_any_schema_write(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        execution = self.config.paths.execution_database
        staging = self.config.paths.staging_database
        original = execution.with_name("execution-original.sqlite3")
        staged_bytes = staging.read_bytes()
        validate_layout = _validate_state_layout
        swapped = False

        def validate_then_swap(
            config,
            *,
            existing: bool,
            include_shared: bool = True,
        ) -> None:
            nonlocal swapped
            validate_layout(
                config,
                existing=existing,
                include_shared=include_shared,
            )
            if not swapped:
                execution.rename(original)
                shutil.copyfile(staging, execution)
                execution.chmod(0o600)
                swapped = True

        with (
            patch(
                "trading_harness.executor_service._validate_state_layout",
                side_effect=validate_then_swap,
            ),
            self.assertRaises(ValidationError),
        ):
            open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertEqual(staged_bytes, execution.read_bytes())

    def test_learning_symlink_swap_cannot_mutate_private_execution_state(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        execution = self.config.paths.execution_database
        learning = self.config.paths.learning_database
        original = learning.with_name("learning-original.sqlite3")
        execution_bytes = execution.read_bytes()
        validate_layout = _validate_state_layout
        swapped = False

        def validate_then_swap(
            config,
            *,
            existing: bool,
            include_shared: bool = True,
        ) -> None:
            nonlocal swapped
            validate_layout(
                config,
                existing=existing,
                include_shared=include_shared,
            )
            if not swapped:
                learning.rename(original)
                learning.symlink_to(execution)
                swapped = True

        with patch(
            "trading_harness.executor_service._validate_state_layout",
            side_effect=validate_then_swap,
        ):
            reopened = open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertIsNone(reopened.learning)
        self.assertEqual(execution_bytes, execution.read_bytes())

    def test_invalid_shared_learning_allows_core_recovery_but_blocks_entry(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        with closing(
            sqlite3.connect(self.config.paths.learning_database)
        ) as connection, connection:
            connection.execute("DROP TRIGGER learning_ledger_no_delete")

        reopened = open_testnet_executor_state(self.config, clock=lambda: AT)
        self.assertIsNone(reopened.learning)
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, reopened.observer.dry_run().step)

        service = build_active_testnet_executor_service(
            state=reopened,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="degraded-learning-instance",
            worker_id="degraded-learning-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        with self.assertRaisesRegex(LearningProjectionError, "entry is blocked"):
            service.learning_projector.require_entry_ready("blocked-command")

    def test_oversized_shared_state_fails_fast_without_blocking_core_recovery(self) -> None:
        initialized = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        learning = self.config.paths.learning_database
        with learning.open("r+b") as stream:
            stream.truncate(MAX_SHARED_STATE_FILE_BYTES + 1)

        assert initialized.learning is not None
        with self.assertRaisesRegex(LedgerIntegrityError, "exceeds its size limit"):
            initialized.learning.events()

        reopened = open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertIsNone(reopened.learning)
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, reopened.observer.dry_run().step)

    def test_noncanonical_shared_payload_cannot_block_core_recovery(self) -> None:
        initialize_testnet_executor_state(self.config, clock=lambda: AT)
        with closing(
            sqlite3.connect(self.config.paths.learning_database)
        ) as connection, connection:
            connection.execute(
                """
                INSERT INTO learning_ledger_events (
                    sequence, event_id, event_type, cycle_id, semantic_key,
                    idempotency_key, occurred_at, recorded_at, payload_json,
                    content_hash, previous_hash, event_hash, schema_version
                ) VALUES (1, ?, 'decision_cycle', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "noncanonical-event",
                    "noncanonical-cycle",
                    "noncanonical-semantic-key",
                    "noncanonical-idempotency-key",
                    AT.isoformat().replace("+00:00", "Z"),
                    AT.isoformat().replace("+00:00", "Z"),
                    '{"x":1.5}',
                    "a" * 64,
                    "0" * 64,
                    "b" * 64,
                ),
            )

        reopened = open_testnet_executor_state(self.config, clock=lambda: AT)

        self.assertIsNone(reopened.learning)
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, reopened.observer.dry_run().step)

    def test_initialize_and_observe_are_credential_free_and_require_existing_state(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not initialized"):
            open_testnet_executor_state(self.config, clock=lambda: AT)

        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)

        for path in (
            self.config.paths.execution_database,
            self.config.paths.nonce_database,
            self.config.paths.daily_loss_database,
            self.config.paths.learning_database,
            self.config.paths.staging_database,
        ):
            self.assertEqual(0, path.stat().st_mode & 0o077)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    self.assertEqual(0, sidecar.stat().st_mode & 0o077)
        self.assertFalse(state.observer.status().active_started)
        self.assertEqual(
            RuntimeStep.STARTUP_RECONCILE, state.observer.dry_run().step
        )
        reopened = open_testnet_executor_state(self.config, clock=lambda: AT)
        self.assertEqual(
            state.runtime_store.read().config_hash,
            reopened.runtime_store.read().config_hash,
        )

    def test_active_service_syncs_exact_loss_then_reconciles_before_ready(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        transport = EmptyLossTransport()
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="active-service-instance",
            worker_id="active-service-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=transport,
        )

        service.start()
        self.assertIsNotNone(
            service.runtime._entry_dispatcher.submission_guard
        )
        first = service.tick()
        second = service.tick()
        third = service.tick()

        self.assertFalse(first.loss_sync_failed)
        self.assertIsNone(first.loss_sync)
        self.assertTrue(first.loss_sync_skipped_for_priority)
        self.assertTrue(second.loss_sync and second.loss_sync.complete)
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, first.runtime_step.step)
        self.assertEqual(RuntimeStep.GATE_RECONCILING, second.runtime_step.step)
        self.assertEqual(RuntimeStep.GATE_READY, third.runtime_step.step)
        self.assertTrue(service.runtime.status().entry_eligible)
        self.assertEqual(
            ["userFillsByTime", "userFunding"],
            [payload["type"] for _, payload in transport.calls],
        )
        self.assertFalse(second.loss_sync_skipped_for_priority)

    def test_loss_transport_failure_blocks_entry_without_skipping_startup_safety(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="offline-instance",
            worker_id="offline-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(fail=True),
        )
        service.start()

        startup = service.tick()
        blocked = service.tick()

        self.assertFalse(startup.loss_sync_failed)
        self.assertTrue(startup.loss_sync_skipped_for_priority)
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, startup.runtime_step.step)
        self.assertEqual(RuntimeStep.LOSS_BLOCKED, blocked.runtime_step.step)
        self.assertTrue(blocked.loss_sync_failed)
        self.assertFalse(service.runtime.status().entry_eligible)

    def test_urgent_runtime_lane_skips_full_history_sync(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="urgent-instance",
            worker_id="urgent-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        service.start()
        preview = service.runtime.dry_run()
        urgent = replace(preview, step=RuntimeStep.SAFETY_ACTION)

        with (
            patch.object(service.runtime, "dry_run", return_value=urgent),
            patch.object(service.runtime, "tick", return_value=urgent),
            patch.object(
                service.loss_synchronizer,
                "synchronize",
                side_effect=AssertionError("urgent work must not wait on history"),
            ) as sync,
            patch.object(
                service.learning_projector,
                "synchronize",
                side_effect=AssertionError("urgent work must not wait on learning"),
            ) as learning_sync,
        ):
            cycle = service.tick()

        self.assertTrue(cycle.loss_sync_skipped_for_priority)
        self.assertTrue(cycle.learning_sync_skipped_for_priority)
        self.assertFalse(sync.called)
        self.assertFalse(learning_sync.called)

    def test_entry_capability_requires_complete_refresh_from_same_tick(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="refresh-capability-instance",
            worker_id="refresh-capability-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        service.start()
        preview = replace(
            service.runtime.dry_run(), step=RuntimeStep.ENTRY_DISPATCH
        )
        blocked = replace(
            preview,
            step=RuntimeStep.LOSS_BLOCKED,
            venue_write_attempted=False,
            entry_eligible=False,
        )
        with (
            patch.object(service.runtime, "dry_run", return_value=preview),
            patch.object(service.runtime, "tick", return_value=blocked) as tick,
            patch.object(
                service.loss_synchronizer,
                "synchronize",
                side_effect=HyperliquidLossSyncError("offline"),
            ),
        ):
            failed = service.tick()
        self.assertTrue(failed.loss_sync_failed)
        tick.assert_called_once_with(entry_refresh_permitted=False)

        with (
            patch.object(service.runtime, "dry_run", return_value=preview),
            patch.object(service.runtime, "tick", return_value=blocked) as tick,
            patch.object(
                service.loss_synchronizer,
                "synchronize",
                return_value=SimpleNamespace(complete=False),
            ),
        ):
            incomplete = service.tick()
        self.assertTrue(incomplete.loss_sync_failed)
        tick.assert_called_once_with(entry_refresh_permitted=False)

    def test_entry_capability_requires_same_tick_learning_sync(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="learning-refresh-instance",
            worker_id="learning-refresh-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        service.start()
        preview = replace(
            service.runtime.dry_run(),
            step=RuntimeStep.ENTRY_DISPATCH,
        )

        with (
            patch.object(service.runtime, "dry_run", return_value=preview),
            patch.object(
                service.loss_synchronizer,
                "synchronize",
                return_value=SimpleNamespace(complete=True),
            ),
            patch.object(
                service.learning_projector,
                "synchronize",
                side_effect=LearningProjectionError("learning full"),
            ),
            patch.object(service.runtime, "tick", return_value=preview) as tick,
        ):
            cycle = service.tick()

        tick.assert_called_once_with(entry_refresh_permitted=False)
        self.assertTrue(cycle.learning_sync_failed)

    def test_post_send_lane_skips_learning_until_reconciliation(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="post-send-learning-instance",
            worker_id="post-send-learning-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        service.start()
        preview = replace(
            service.runtime.dry_run(),
            step=RuntimeStep.ENTRY_DISPATCH,
        )
        sent = replace(preview, venue_write_attempted=True)
        learning_report = SimpleNamespace()

        with (
            patch.object(service.runtime, "dry_run", return_value=preview),
            patch.object(
                service.loss_synchronizer,
                "synchronize",
                return_value=SimpleNamespace(complete=True),
            ),
            patch.object(
                service.learning_projector,
                "synchronize",
                return_value=learning_report,
            ) as learning_sync,
            patch.object(service.runtime, "tick", return_value=sent),
        ):
            cycle = service.tick()

        self.assertEqual(1, learning_sync.call_count)
        self.assertTrue(cycle.learning_sync_skipped_for_priority)

    def test_idle_preview_cannot_authorize_command_admitted_before_tick(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        service = build_active_testnet_executor_service(
            state=state,
            wallet=FakeWallet(self.config.api_wallet_address),
            recovery_secret=b"r" * 32,
            instance_id="idle-race-instance",
            worker_id="idle-race-worker",
            clock=lambda: AT,
            policy=self.policy,
            account_reader=lambda _address, _network: self.snapshot,
            market_reader=lambda _symbol, _network: {},
            info_transport=EmptyLossTransport(),
        )
        service.start()
        preview = replace(service.runtime.dry_run(), step=RuntimeStep.IDLE)
        blocked = replace(
            preview,
            step=RuntimeStep.LOSS_BLOCKED,
            venue_write_attempted=False,
            entry_eligible=False,
        )
        service._last_loss_sync_at = AT
        with (
            patch.object(service.runtime, "dry_run", return_value=preview),
            patch.object(service.runtime, "tick", return_value=blocked) as tick,
            patch.object(service.loss_synchronizer, "synchronize") as sync,
        ):
            service.tick()
        self.assertFalse(sync.called)
        tick.assert_called_once_with(entry_refresh_permitted=False)

    def test_wrong_wallet_secret_policy_and_insecure_directory_fail_closed(self) -> None:
        state = initialize_testnet_executor_state(self.config, clock=lambda: AT)
        common = {
            "state": state,
            "recovery_secret": b"r" * 32,
            "instance_id": "bad-instance",
            "worker_id": "bad-worker",
            "clock": lambda: AT,
            "policy": self.policy,
        }
        with self.assertRaisesRegex(ValidationError, "wallet"):
            build_active_testnet_executor_service(
                wallet=FakeWallet("0x" + "9" * 40), **common
            )
        with self.assertRaisesRegex(ValidationError, "32 bytes"):
            build_active_testnet_executor_service(
                wallet=FakeWallet(self.config.api_wallet_address),
                **{**common, "recovery_secret": b"short"},
            )
        with self.assertRaisesRegex(ValidationError, "risk policy"):
            build_active_testnet_executor_service(
                wallet=FakeWallet(self.config.api_wallet_address),
                **{**common, "policy": RiskSizingPolicy()},
            )

        other_root = self.root / "insecure"
        other_root.mkdir(mode=0o755)
        insecure = parse_executor_config(
            config_text(other_root, self.policy.policy_hash), environ={}
        )
        insecure.paths.execution_database.parent.chmod(0o755)
        with self.assertRaisesRegex(ValidationError, "0700"):
            initialize_testnet_executor_state(insecure, clock=lambda: AT)

    def test_real_wallet_checksum_case_is_normalized_before_config_comparison(self) -> None:
        checksummed = "0x" + "Aa" * 20
        self.assertEqual(checksummed.lower(), _wallet_address(FakeWallet(checksummed)))


if __name__ == "__main__":
    unittest.main()
