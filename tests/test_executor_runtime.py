from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.daily_loss import DailyLossBinding, DailyLossLedger
from trading_harness.errors import EntrySubmissionRevoked, StateConflict
from trading_harness.execution_store import (
    CommandRecord,
    ExecutionStore,
    IncidentRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryCommand,
    RecoveryOutbox,
)
from trading_harness.execution_work_scanner import (
    REQUIRED_EXECUTION_STORE_METHODS,
    ExecutionWorkScanner,
)
from trading_harness.executor_config import parse_executor_config
from trading_harness.executor_runtime import (
    ExecutorRuntime,
    HandlerDisposition,
    HandlerResult,
    RuntimeOrchestrationError,
    RuntimeIntegrityFailure,
    RuntimeStep,
)
from trading_harness import executor_runtime
from trading_harness.executor_runtime_store import (
    ExecutorRuntimeStore,
    RuntimeLeaseState,
)
from trading_harness.executor_status import ExecutorRiskGate


NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def config_text(root: Path) -> str:
    for name in ("execution", "nonce", "daily-loss", "learning", "socket"):
        directory = root / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return f'''schema_version = 2
environment = "testnet"
venue = "hyperliquid"
node_id = "runtime-node"
executor_uid = {os.geteuid()}
research_uid = {os.geteuid() + 1}
control_uid = {os.geteuid() + 2}
account_id = "runtime-account"
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
service = "runtime-keychain-service"
account = "runtime-keychain-account"
timeout_seconds = 5

[approval_credential]
provider = "macos_keychain_generic_password"
service = "runtime-approval-service"
account = "runtime-approval-account"
timeout_seconds = 5

[recovery_credential]
provider = "macos_keychain_generic_password"
service = "runtime-recovery-service"
account = "runtime-recovery-account"
timeout_seconds = 5

[grant_credential]
provider = "macos_keychain_generic_password"
service = "runtime-grant-service"
account = "runtime-grant-account"
timeout_seconds = 5

[paths]
execution_database = "{root / 'execution' / 'execution.sqlite3'}"
nonce_database = "{root / 'nonce' / 'nonce.sqlite3'}"
daily_loss_database = "{root / 'daily-loss' / 'daily-loss.sqlite3'}"
learning_database = "{root / 'learning' / 'learning.sqlite3'}"
staging_database = "{root / 'learning' / 'staging.sqlite3'}"
control_socket = "{root / 'socket' / 'control.sock'}"
'''


def command(command_id: str, state: str) -> CommandRecord:
    return CommandRecord(
        command_id=command_id,
        ticket_hash=HASH,
        plan_hash=HASH,
        approval_id="approval-" + command_id,
        state=state,
        reserved_loss=Decimal("1"),
        reserved_notional=Decimal("10"),
        created_at=NOW,
        updated_at=NOW,
        terminal_at=NOW if state == "terminal" else None,
        revision=1,
    )


def outbox(command_id: str, state: str) -> OutboxRecord:
    return OutboxRecord(
        command_id=command_id,
        state=state,
        worker_id=None,
        fencing_token=0,
        claimed_at=None,
        lease_expires_at=None,
        current_attempt_id=None,
        attempt_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


def recovery(recovery_id: str, state: str, *, priority: int) -> RecoveryCommand:
    return RecoveryCommand(
        recovery_command_id=recovery_id,
        permit_id="permit-" + recovery_id,
        parent_command_id="parent-" + recovery_id,
        incident_id="incident-" + recovery_id,
        kind=("noop_fence" if priority == 0 else "cancel_by_cloid"),
        priority=priority,
        source_hash=HASH,
        preflight_hash=HASH,
        recovery_hash=HASH,
        recovery_material_json="{}",
        recovery_material_hash=HASH,
        safety_policy_hash=HASH,
        original_attempt_id=("attempt" if priority == 0 else None),
        original_nonce=(1 if priority == 0 else None),
        state=state,
        created_at=NOW,
        updated_at=NOW,
        terminal_at=NOW if state == "terminal" else None,
        revision=1,
    )


def recovery_outbox(recovery_id: str, state: str) -> RecoveryOutbox:
    return RecoveryOutbox(
        recovery_command_id=recovery_id,
        state=state,
        worker_id=None,
        fencing_token=0,
        claimed_at=None,
        lease_expires_at=None,
        current_attempt_id=None,
        attempt_count=0,
        created_at=NOW,
        updated_at=NOW,
    )


class WorkState:
    def __init__(self) -> None:
        self.commands: list[CommandRecord] = []
        self.outboxes: list[OutboxRecord] = []
        self.recoveries: list[RecoveryCommand] = []
        self.recovery_outboxes: list[RecoveryOutbox] = []
        self.positions: list[PositionRecord] = []
        self.protections: list[ProtectionRecord] = []
        self.incidents: list[IncidentRecord] = []

    def terminalize_command(self, command_id: str) -> None:
        self.commands = [
            replace(item, state="terminal", terminal_at=NOW, revision=item.revision + 1)
            if item.command_id == command_id
            else item
            for item in self.commands
        ]
        self.outboxes = [
            replace(item, state="terminal") if item.command_id == command_id else item
            for item in self.outboxes
        ]

    def terminalize_recovery(self, recovery_id: str) -> None:
        self.recoveries = [
            replace(item, state="terminal", terminal_at=NOW, revision=item.revision + 1)
            if item.recovery_command_id == recovery_id
            else item
            for item in self.recoveries
        ]
        self.recovery_outboxes = [
            replace(item, state="terminal")
            if item.recovery_command_id == recovery_id
            else item
            for item in self.recovery_outboxes
        ]

    def install(self, stack: ExitStack) -> None:
        values = {
            "list_commands": lambda: tuple(self.commands),
            "list_outboxes": lambda: tuple(self.outboxes),
            "list_recovery_commands": lambda: tuple(self.recoveries),
            "list_recovery_outboxes": lambda: tuple(self.recovery_outboxes),
            "list_positions": lambda: tuple(self.positions),
            "list_protections": lambda: tuple(self.protections),
            "list_incidents": lambda: tuple(self.incidents),
        }

        def method(name: str):
            return lambda _store: values[name]()

        for requirement in REQUIRED_EXECUTION_STORE_METHODS:
            stack.enter_context(
                patch.object(
                    ExecutionStore,
                    requirement.name,
                    new=method(requirement.name),
                    create=True,
                )
            )


class StartupPort:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def reconcile_startup(self) -> HandlerResult:
        self.log.append("startup")
        return HandlerResult(HandlerDisposition.COMPLETE, False)


class ReconcilePort:
    def __init__(self, label: str, log: list[str], callback) -> None:
        self.label = label
        self.log = log
        self.callback = callback

    def reconcile_next(self) -> HandlerResult:
        self.log.append(self.label)
        self.callback()
        return HandlerResult(HandlerDisposition.PROGRESSED, True)


class ProtectionPort:
    def __init__(self, log: list[str], callback) -> None:
        self.log = log
        self.callback = callback

    def inspect_next(self) -> HandlerResult:
        self.log.append("protection")
        self.callback()
        return HandlerResult(HandlerDisposition.PROGRESSED, True)


class SafetyPort:
    def __init__(self, log: list[str], callback) -> None:
        self.log = log
        self.callback = callback

    def act_next(self) -> HandlerResult:
        self.log.append("safety")
        self.callback()
        return HandlerResult(HandlerDisposition.PROGRESSED, True)


class PreparedSafetyPort:
    """Model a read-only safety pass that prepared existing queued work."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def act_next(self) -> HandlerResult:
        self.log.append("safety_prepared")
        return HandlerResult(HandlerDisposition.NO_WORK, False)


class DispatchEvidence:
    venue_write_attempted = True


class RecoveryDispatchPort:
    def __init__(self, log: list[str], callback) -> None:
        self.log = log
        self.callback = callback

    def dispatch_next(self):
        self.log.append("recovery_dispatch")
        self.callback()
        return DispatchEvidence()


class EntryDispatchPort:
    def __init__(self, log: list[str], callback) -> None:
        self.log = log
        self.callback = callback

    def dispatch_next(self, worker_id: str):
        self.log.append("entry_dispatch:" + worker_id)
        self.callback()
        return DispatchEvidence()


class PoisonPorts:
    def __getattribute__(self, name: str):
        if name.startswith("_"):
            return object.__getattribute__(self, name)

        def fail(*_args, **_kwargs):
            raise AssertionError("dry-run/status touched injected port: " + name)

        return fail


class ExecutorRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).absolute()
        self.config = parse_executor_config(config_text(self.root), environ={})
        self.clock = FakeClock()
        self.execution_store = ExecutionStore(
            self.config.paths.execution_database,
            environment=self.config.environment,
            account_id=self.config.account_id,
            max_reserved_loss=self.config.max_reserved_loss,
            max_reserved_notional=self.config.max_reserved_notional,
        )
        self.runtime_store = ExecutorRuntimeStore(self.config, clock=self.clock)
        binding = DailyLossBinding(
            account_id=self.config.account_id,
            environment=self.config.environment,
            config_hash=self.config.config_hash,
            daily_loss_limit=self.config.daily_loss_limit,
            settlement_currency=self.config.settlement_currency,
        )
        self.daily_loss = DailyLossLedger(
            self.config.paths.daily_loss_database,
            binding=binding,
            clock=self.clock,
        )
        start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        for source in ("fills", "funding"):
            self.daily_loss.record_coverage(
                coverage_id=source + "-coverage",
                source=source,
                covered_from=start,
                covered_through=NOW,
                source_cursor_hash=digest(source),
            )
        self.work = WorkState()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.work.install(self.stack)
        self.scanner = ExecutionWorkScanner(
            self.execution_store, clock=self.clock
        )

    def runtime(self, *, log: list[str] | None = None) -> ExecutorRuntime:
        selected_log = [] if log is None else log
        return ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(selected_log),
            recovery_reconciler=ReconcilePort(
                "recovery_reconcile", selected_log, lambda: None
            ),
            parent_reconciler=ReconcilePort(
                "parent_reconcile", selected_log, lambda: None
            ),
            protection_inspector=ProtectionPort(selected_log, lambda: None),
            safety_handler=SafetyPort(selected_log, lambda: None),
            recovery_dispatcher=RecoveryDispatchPort(selected_log, lambda: None),
            entry_dispatcher=EntryDispatchPort(selected_log, lambda: None),
            clock=self.clock,
            lease_seconds=30,
        )

    def test_startup_reconciliation_precedes_ready_and_entry(self) -> None:
        log: list[str] = []
        runtime = self.runtime(log=log)
        started = runtime.start()
        self.assertEqual(started.declared_risk_gate, ExecutorRiskGate.RECONCILING)
        self.assertNotEqual(started.effective_risk_gate, ExecutorRiskGate.READY)

        first = runtime.tick()
        self.assertEqual(first.step, RuntimeStep.STARTUP_RECONCILE)
        self.assertEqual(log, ["startup"])
        self.assertEqual(
            self.runtime_store.read().declared_risk_gate,
            ExecutorRiskGate.RECONCILING,
        )
        second = runtime.tick()
        self.assertEqual(second.step, RuntimeStep.GATE_READY)
        self.assertEqual(
            self.runtime_store.read().effective_risk_gate,
            ExecutorRiskGate.READY,
        )

    def test_entry_requires_same_cycle_loss_refresh_capability(self) -> None:
        log: list[str] = []
        self.work.commands = [command("entry-command", "queued")]
        self.work.outboxes = [outbox("entry-command", "queued")]
        runtime = self.runtime(log=log)
        runtime.start()
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, runtime.tick().step)
        self.assertEqual(RuntimeStep.GATE_READY, runtime.tick().step)

        blocked = runtime.tick()
        sent = runtime.tick(entry_refresh_permitted=True)

        self.assertEqual(RuntimeStep.LOSS_BLOCKED, blocked.step)
        self.assertFalse(blocked.venue_write_attempted)
        self.assertEqual(RuntimeStep.ENTRY_DISPATCH, sent.step)
        self.assertIn("entry_dispatch:runtime-worker", log)

    def test_shutdown_during_entry_preparation_revokes_final_send_guard(self) -> None:
        self.work.commands = [command("entry-command", "queued")]
        self.work.outboxes = [outbox("entry-command", "queued")]
        runtime = self.runtime(log=[])

        class ShutdownPort:
            def dispatch_next(self, _worker_id: str):
                runtime.request_shutdown()
                try:
                    with runtime.entry_submission_guard():
                        raise AssertionError("revoked guard unexpectedly entered")
                except EntrySubmissionRevoked:
                    class Revoked:
                        venue_write_attempted = False

                    return Revoked()

        runtime._entry_dispatcher = ShutdownPort()
        runtime.start()
        runtime.tick()
        runtime.tick()

        result = runtime.tick(entry_refresh_permitted=True)

        self.assertTrue(runtime.shutdown_requested)
        self.assertEqual(RuntimeStep.ENTRY_DISPATCH, result.step)
        self.assertFalse(result.venue_write_attempted)
        self.assertFalse(self.runtime_store.read().manual_halt)

    def test_stale_runtime_cannot_borrow_replacement_ready_lease(self) -> None:
        stale = self.runtime(log=[])
        stale.start()
        stale.tick()
        stale.tick()
        with stale._entry_submission_lock:
            stale._entry_submission_active = True

        self.clock.now += timedelta(seconds=31)
        replacement = self.runtime_store.acquire(
            instance_id="replacement-runtime", lease_seconds=30
        )
        replacement = self.runtime_store.heartbeat(
            instance_id="replacement-runtime",
            fencing_token=replacement.fencing_token,
            lease_seconds=30,
        )
        replacement = self.runtime_store.transition(
            instance_id="replacement-runtime",
            fencing_token=replacement.fencing_token,
            process_state="running",
            risk_gate="reconciling",
        )
        self.runtime_store.transition(
            instance_id="replacement-runtime",
            fencing_token=replacement.fencing_token,
            risk_gate="ready",
        )

        with self.assertRaisesRegex(EntrySubmissionRevoked, "not active"):
            with stale.entry_submission_guard():
                raise AssertionError("stale runtime borrowed replacement lease")

    def test_strict_priority_executes_one_lane_before_lower_work(self) -> None:
        log: list[str] = []
        self.work.commands = [
            command("parent-command", "reconciling"),
            command("entry-command", "queued"),
        ]
        self.work.outboxes = [
            outbox("parent-command", "reconciling"),
            outbox("entry-command", "queued"),
        ]
        self.work.recoveries = [
            recovery("reconcile-recovery", "reconciling", priority=0),
            recovery("dispatch-recovery", "queued", priority=1),
        ]
        self.work.recovery_outboxes = [
            recovery_outbox("reconcile-recovery", "reconciling"),
            recovery_outbox("dispatch-recovery", "queued"),
        ]
        self.work.protections = [
            ProtectionRecord(
                "parent-command",
                "ETH-PERP",
                "under_protected",
                Decimal("1"),
                Decimal("0.5"),
                "stop",
                NOW,
                1,
            )
        ]
        self.work.incidents = [
            IncidentRecord(
                "open-incident",
                "parent-command",
                "under_protected",
                "critical",
                "open",
                NOW,
                NOW,
                1,
                {},
            )
        ]

        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(log),
            recovery_reconciler=ReconcilePort(
                "recovery_reconcile",
                log,
                lambda: self.work.terminalize_recovery("reconcile-recovery"),
            ),
            parent_reconciler=ReconcilePort(
                "parent_reconcile",
                log,
                lambda: self.work.terminalize_command("parent-command"),
            ),
            protection_inspector=ProtectionPort(
                log,
                lambda: setattr(self.work, "protections", []),
            ),
            safety_handler=SafetyPort(
                log,
                lambda: setattr(self.work, "incidents", []),
            ),
            recovery_dispatcher=RecoveryDispatchPort(
                log,
                lambda: self.work.terminalize_recovery("dispatch-recovery"),
            ),
            entry_dispatcher=EntryDispatchPort(
                log,
                lambda: self.work.terminalize_command("entry-command"),
            ),
            clock=self.clock,
        )
        runtime.start()
        steps = [
            runtime.tick(entry_refresh_permitted=True).step
            for _ in range(8)
        ]
        self.assertEqual(
            steps,
            [
                RuntimeStep.RECOVERY_RECONCILE,
                RuntimeStep.RECOVERY_DISPATCH,
                RuntimeStep.PARENT_RECONCILE,
                RuntimeStep.PROTECTION_CHECK,
                RuntimeStep.STARTUP_RECONCILE,
                RuntimeStep.GATE_READY,
                RuntimeStep.ENTRY_DISPATCH,
                RuntimeStep.IDLE,
            ],
        )
        self.assertEqual(
            log,
            [
                "recovery_reconcile",
                "safety",
                "recovery_dispatch",
                "parent_reconcile",
                "protection",
                "startup",
                "entry_dispatch:runtime-worker",
            ],
        )

    def test_incomplete_loss_blocks_entry_but_not_recovery_reconciliation(self) -> None:
        incomplete_root = self.root / "incomplete"
        incomplete_root.mkdir()
        incomplete_config = parse_executor_config(
            config_text(incomplete_root), environ={}
        )
        incomplete_binding = DailyLossBinding(
            account_id=incomplete_config.account_id,
            environment=incomplete_config.environment,
            config_hash=incomplete_config.config_hash,
            daily_loss_limit=incomplete_config.daily_loss_limit,
            settlement_currency=incomplete_config.settlement_currency,
        )
        incomplete_loss = DailyLossLedger(
            incomplete_config.paths.daily_loss_database,
            binding=incomplete_binding,
            clock=self.clock,
        )
        incomplete_execution = ExecutionStore(
            incomplete_config.paths.execution_database,
            environment=incomplete_config.environment,
            account_id=incomplete_config.account_id,
            max_reserved_loss=incomplete_config.max_reserved_loss,
            max_reserved_notional=incomplete_config.max_reserved_notional,
        )
        scanner = ExecutionWorkScanner(incomplete_execution, clock=self.clock)
        runtime_store = ExecutorRuntimeStore(incomplete_config, clock=self.clock)
        self.work.recoveries = [recovery("recover", "reconciling", priority=0)]
        self.work.recovery_outboxes = [
            recovery_outbox("recover", "reconciling")
        ]
        self.work.commands = [command("entry", "queued")]
        self.work.outboxes = [outbox("entry", "queued")]
        log: list[str] = []
        runtime = ExecutorRuntime(
            runtime_store=runtime_store,
            work_scanner=scanner,
            daily_loss=incomplete_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(log),
            recovery_reconciler=ReconcilePort(
                "recovery_reconcile",
                log,
                lambda: self.work.terminalize_recovery("recover"),
            ),
            parent_reconciler=ReconcilePort("parent", log, lambda: None),
            protection_inspector=ProtectionPort(log, lambda: None),
            safety_handler=SafetyPort(log, lambda: None),
            recovery_dispatcher=RecoveryDispatchPort(log, lambda: None),
            entry_dispatcher=EntryDispatchPort(log, lambda: None),
            clock=self.clock,
        )
        runtime.start()
        self.assertEqual(runtime.tick().step, RuntimeStep.RECOVERY_RECONCILE)
        self.assertEqual(runtime.tick().step, RuntimeStep.STARTUP_RECONCILE)
        self.assertEqual(runtime.tick().step, RuntimeStep.LOSS_BLOCKED)
        self.assertNotIn("entry_dispatch:runtime-worker", log)

    def test_prepared_existing_safety_work_falls_through_to_recovery_dispatch(self) -> None:
        log: list[str] = []
        self.work.recoveries = [recovery("prepared-recovery", "queued", priority=1)]
        self.work.recovery_outboxes = [
            recovery_outbox("prepared-recovery", "queued")
        ]
        self.work.incidents = [
            IncidentRecord(
                "prepared-incident",
                "parent-prepared",
                "under_protected",
                "critical",
                "open",
                NOW,
                NOW,
                1,
                {},
            )
        ]
        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(log),
            recovery_reconciler=ReconcilePort("recovery", log, lambda: None),
            parent_reconciler=ReconcilePort("parent", log, lambda: None),
            protection_inspector=ProtectionPort(log, lambda: None),
            safety_handler=PreparedSafetyPort(log),
            recovery_dispatcher=RecoveryDispatchPort(
                log,
                lambda: self.work.terminalize_recovery("prepared-recovery"),
            ),
            entry_dispatcher=EntryDispatchPort(log, lambda: None),
            clock=self.clock,
        )
        runtime.start()

        result = runtime.tick()

        self.assertEqual(RuntimeStep.RECOVERY_DISPATCH, result.step)
        self.assertEqual(["safety_prepared", "recovery_dispatch"], log)

    def test_newly_queued_safety_recovery_dispatches_in_the_same_tick(self) -> None:
        log: list[str] = []
        self.work.incidents = [
            IncidentRecord(
                "urgent-incident",
                "parent-urgent",
                "under_protected",
                "critical",
                "open",
                NOW,
                NOW,
                1,
                {},
            )
        ]

        def queue_recovery() -> None:
            self.work.recoveries = [
                recovery("urgent-recovery", "queued", priority=0)
            ]
            self.work.recovery_outboxes = [
                recovery_outbox("urgent-recovery", "queued")
            ]

        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(log),
            recovery_reconciler=ReconcilePort("recovery", log, lambda: None),
            parent_reconciler=ReconcilePort("parent", log, lambda: None),
            protection_inspector=ProtectionPort(log, lambda: None),
            safety_handler=SafetyPort(log, queue_recovery),
            recovery_dispatcher=RecoveryDispatchPort(
                log,
                lambda: self.work.terminalize_recovery("urgent-recovery"),
            ),
            entry_dispatcher=EntryDispatchPort(log, lambda: None),
            clock=self.clock,
        )
        runtime.start()

        result = runtime.tick()

        self.assertEqual(RuntimeStep.RECOVERY_DISPATCH, result.step)
        self.assertEqual(["safety", "recovery_dispatch"], log)

    def test_status_and_dry_run_never_call_ports_or_start_runtime(self) -> None:
        poison = PoisonPorts()
        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=poison,
            recovery_reconciler=poison,
            parent_reconciler=poison,
            protection_inspector=poison,
            safety_handler=poison,
            recovery_dispatcher=poison,
            entry_dispatcher=poison,
            clock=self.clock,
        )
        status = runtime.status()
        dry = runtime.dry_run()
        self.assertFalse(status.active_started)
        self.assertEqual(status.runtime.lease_state, RuntimeLeaseState.NOT_STARTED)
        self.assertEqual(dry.step, RuntimeStep.STARTUP_RECONCILE)
        self.assertTrue(dry.dry_run)
        self.assertFalse(dry.venue_write_attempted)
        self.assertEqual(
            self.runtime_store.read().lease_state,
            RuntimeLeaseState.NOT_STARTED,
        )

    def test_stale_composed_clock_fails_read_only_path_closed(self) -> None:
        runtime_clock = FakeClock(NOW.replace(second=10))
        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            clock=runtime_clock,
        )
        with self.assertRaises(RuntimeIntegrityFailure):
            runtime.dry_run()

    def test_active_start_requires_all_ports_before_acquiring_lease(self) -> None:
        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            clock=self.clock,
        )
        with self.assertRaises(RuntimeOrchestrationError):
            runtime.start()
        self.assertEqual(
            self.runtime_store.read().lease_state,
            RuntimeLeaseState.NOT_STARTED,
        )

    def test_sigterm_shutdown_halts_entry_drains_safety_then_releases(self) -> None:
        log: list[str] = []
        recovery_reconciler = ReconcilePort(
            "recovery_reconcile",
            log,
            lambda: self.work.terminalize_recovery("recover-reconcile"),
        )
        parent_reconciler = ReconcilePort(
            "parent_reconcile",
            log,
            lambda: self.work.terminalize_command("parent"),
        )
        safety_handler = SafetyPort(
            log,
            lambda: setattr(self.work, "incidents", []),
        )
        recovery_dispatcher = RecoveryDispatchPort(
            log,
            lambda: self.work.terminalize_recovery("recover-dispatch"),
        )
        runtime = ExecutorRuntime(
            runtime_store=self.runtime_store,
            work_scanner=self.scanner,
            daily_loss=self.daily_loss,
            instance_id="runtime-instance",
            worker_id="runtime-worker",
            startup_reconciler=StartupPort(log),
            recovery_reconciler=recovery_reconciler,
            parent_reconciler=parent_reconciler,
            protection_inspector=ProtectionPort(log, lambda: None),
            safety_handler=safety_handler,
            recovery_dispatcher=recovery_dispatcher,
            entry_dispatcher=EntryDispatchPort(log, lambda: None),
            clock=self.clock,
        )
        runtime.start()
        self.assertEqual(runtime.tick().step, RuntimeStep.STARTUP_RECONCILE)
        self.assertEqual(runtime.tick().step, RuntimeStep.GATE_READY)
        log.clear()

        self.work.commands = [
            command("parent", "reconciling"),
            command("entry", "queued"),
        ]
        self.work.outboxes = [
            outbox("parent", "reconciling"),
            outbox("entry", "queued"),
        ]
        self.work.recoveries = [
            recovery("recover-reconcile", "reconciling", priority=0),
            recovery("recover-dispatch", "queued", priority=1),
        ]
        self.work.recovery_outboxes = [
            recovery_outbox("recover-reconcile", "reconciling"),
            recovery_outbox("recover-dispatch", "queued"),
        ]
        self.work.incidents = [
            IncidentRecord(
                "incident",
                "parent",
                "risk",
                "critical",
                "open",
                NOW,
                NOW,
                1,
                {},
            )
        ]
        runtime.request_shutdown()
        waiting = runtime.tick()
        self.assertEqual(waiting.step, RuntimeStep.SHUTDOWN_DRAIN)
        self.assertEqual(
            self.runtime_store.read().declared_risk_gate,
            ExecutorRiskGate.HALTED,
        )
        report = runtime.shutdown(max_drain_steps=4)
        self.assertTrue(report.clean)
        self.assertTrue(report.released)
        self.assertEqual(report.remaining_safety_work, 0)
        self.assertEqual(
            [item.step for item in report.drain_steps],
            [
                RuntimeStep.RECOVERY_RECONCILE,
                RuntimeStep.RECOVERY_DISPATCH,
                RuntimeStep.PARENT_RECONCILE,
            ],
        )
        self.assertNotIn("entry_dispatch:runtime-worker", log)
        self.assertEqual(
            self.runtime_store.read().lease_state,
            RuntimeLeaseState.RELEASED,
        )

    def test_daily_loss_limit_halts_until_a_new_day_has_complete_coverage(self) -> None:
        self.daily_loss.record_fee(
            event_id="loss-limit",
            source_ref="fill-loss-limit",
            occurred_at=NOW,
            fee=Decimal("25"),
        )
        runtime = self.runtime()
        runtime.start()
        result = runtime.tick()
        self.assertEqual(result.step, RuntimeStep.STARTUP_RECONCILE)
        blocked = runtime.tick()
        self.assertEqual(blocked.step, RuntimeStep.LOSS_BLOCKED)
        status = self.runtime_store.read()
        self.assertFalse(status.manual_halt)
        self.assertIsNone(status.manual_halt_reason)
        self.assertEqual(status.effective_risk_gate, ExecutorRiskGate.HALTED)

        runtime.shutdown(max_drain_steps=0)
        self.clock.now += timedelta(days=1)
        start = self.clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        for source in ("fills", "funding"):
            self.daily_loss.record_coverage(
                coverage_id=f"next-day-{source}",
                source=source,
                covered_from=start,
                covered_through=self.clock.now,
                source_cursor_hash=digest(f"next-day-{source}"),
            )
        next_runtime = self.runtime()
        next_runtime.start()
        self.assertEqual(RuntimeStep.STARTUP_RECONCILE, next_runtime.tick().step)
        self.assertEqual(RuntimeStep.GATE_READY, next_runtime.tick().step)

    def test_bounded_shutdown_releases_even_with_remaining_work(self) -> None:
        log: list[str] = []
        self.work.recoveries = [recovery("one", "reconciling", priority=0)]
        self.work.recovery_outboxes = [recovery_outbox("one", "reconciling")]
        runtime = self.runtime(log=log)
        runtime.start()
        report = runtime.shutdown(max_drain_steps=1)
        self.assertFalse(report.clean)
        self.assertTrue(report.released)
        self.assertEqual(report.remaining_safety_work, 1)
        self.assertEqual(len(report.drain_steps), 1)
        self.assertNotIn("entry_dispatch:runtime-worker", log)
        self.assertEqual(
            self.runtime_store.read().lease_state,
            RuntimeLeaseState.RELEASED,
        )

    def test_runtime_module_owns_no_credential_signer_or_network_capability(self) -> None:
        source = inspect.getsource(executor_runtime)
        for forbidden in (
            "credential_provider",
            "load_wallet",
            "submit_signed_action",
            "hyperliquid_transport",
            "requests",
            "urllib",
            "socket",
            "sqlite3",
        ):
            self.assertNotIn(forbidden, source)
        for method in (ExecutorRuntime.status, ExecutorRuntime.dry_run):
            method_source = inspect.getsource(method)
            self.assertNotIn("_entry_dispatcher", method_source)
            self.assertNotIn("_recovery_dispatcher", method_source)


if __name__ == "__main__":
    unittest.main()
