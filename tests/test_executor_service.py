from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.errors import ValidationError
from trading_harness.executor_config import parse_executor_config
from trading_harness.executor_runtime import RuntimeStep
from trading_harness.executor_service import (
    _wallet_address,
    build_active_testnet_executor_service,
    initialize_testnet_executor_state,
    open_testnet_executor_state,
)
from trading_harness.hyperliquid_account import fetch_account_snapshot
from trading_harness.hyperliquid_loss_sync import HyperliquidLossSyncError
from trading_harness.planning import RiskSizingPolicy
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
        ):
            cycle = service.tick()

        self.assertTrue(cycle.loss_sync_skipped_for_priority)
        self.assertFalse(sync.called)

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
        with self.assertRaisesRegex(ValidationError, "0700"):
            initialize_testnet_executor_state(insecure, clock=lambda: AT)

    def test_real_wallet_checksum_case_is_normalized_before_config_comparison(self) -> None:
        checksummed = "0x" + "Aa" * 20
        self.assertEqual(checksummed.lower(), _wallet_address(FakeWallet(checksummed)))


if __name__ == "__main__":
    unittest.main()
