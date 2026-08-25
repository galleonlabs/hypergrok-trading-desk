"""Single-threaded orchestration for the isolated TESTNET executor.

This module composes already-reviewed stores and narrow injected ports.  It
does not load credentials, sign, call a venue, inspect SQLite directly, or
create capital authority.  In active mode it runs one strict-priority step at
a time:

1. verify local integrity and daily-loss coverage;
2. reconcile recovery commands;
3. reconcile parent commands and inspect protection;
4. derive/queue one bounded account-safety action;
5. dispatch one recovery action; and
6. dispatch one protected entry only when every higher lane is empty.

Startup remains ``reconciling`` until an injected startup reconciler proves a
complete account view.  Shutdown disables entry immediately, drains a bounded
number of reconciliation/safety steps, then persists stopping, stopped, and
released states.  Status and dry-run paths call no injected handler or
dispatcher, so they cannot cause credential access or a venue write.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import threading
from typing import Iterator, Protocol

from .canonical import domain_hash
from .daily_loss import DailyLossLedger, DailyLossSnapshot
from .errors import (
    EntrySubmissionRevoked,
    HarnessError,
    StateConflict,
    StorageError,
    ValidationError,
)
from .execution_work_scanner import (
    ExecutionWorkKind,
    ExecutionWorkScan,
    ExecutionWorkScanner,
)
from .executor_runtime_store import (
    ExecutorRuntimeReadModel,
    ExecutorRuntimeStore,
    ManualHaltReason,
    RuntimeLease,
    RuntimeLeaseState,
)
from .executor_status import ExecutorProcessState, ExecutorRiskGate


Clock = Callable[[], datetime]
MAX_LOCAL_VIEW_AGE_SECONDS = 5


class RuntimeOrchestrationError(HarnessError):
    """The runtime could not safely continue its serialized loop."""


class RuntimeNotStarted(RuntimeOrchestrationError):
    pass


class RuntimeIntegrityFailure(RuntimeOrchestrationError):
    pass


class HandlerDisposition(str, Enum):
    NO_WORK = "no_work"
    PROGRESSED = "progressed"
    WAITING = "waiting"
    COMPLETE = "complete"


class RuntimeStep(str, Enum):
    INTEGRITY_BLOCKED = "integrity_blocked"
    LOSS_BLOCKED = "loss_blocked"
    RECOVERY_RECONCILE = "recovery_reconcile"
    RECOVERY_WAIT = "recovery_wait"
    PARENT_RECONCILE = "parent_reconcile"
    PARENT_WAIT = "parent_wait"
    PROTECTION_CHECK = "protection_check"
    SAFETY_ACTION = "safety_action"
    RECOVERY_DISPATCH = "recovery_dispatch"
    STARTUP_RECONCILE = "startup_reconcile"
    GATE_RECONCILING = "gate_reconciling"
    GATE_READY = "gate_ready"
    ENTRY_DISPATCH = "entry_dispatch"
    IDLE = "idle"
    SHUTDOWN_DRAIN = "shutdown_drain"


@dataclass(frozen=True, slots=True)
class HandlerResult:
    disposition: HandlerDisposition
    local_state_changed: bool
    venue_write_attempted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, HandlerDisposition):
            raise TypeError("disposition must be HandlerDisposition")
        if type(self.local_state_changed) is not bool:
            raise TypeError("local_state_changed must be bool")
        if type(self.venue_write_attempted) is not bool:
            raise TypeError("venue_write_attempted must be bool")
        if self.venue_write_attempted:
            raise ValidationError(
                "reconciliation and safety handlers may not perform venue writes"
            )
        if (
            self.disposition is HandlerDisposition.PROGRESSED
            and not self.local_state_changed
        ):
            raise ValidationError("progressed handler result must change local state")
        if (
            self.disposition
            in {
                HandlerDisposition.NO_WORK,
                HandlerDisposition.WAITING,
            }
            and self.local_state_changed
        ):
            raise ValidationError("non-progress handler result may not claim a change")


class StartupReconciliationHandler(Protocol):
    def reconcile_startup(self) -> HandlerResult: ...


class RecoveryReconciliationHandler(Protocol):
    def reconcile_next(self) -> HandlerResult: ...


class ParentReconciliationHandler(Protocol):
    def reconcile_next(self) -> HandlerResult: ...


class ProtectionInspectionHandler(Protocol):
    def inspect_next(self) -> HandlerResult: ...


class SafetyActionHandler(Protocol):
    def act_next(self) -> HandlerResult: ...


class EntryDispatcherPort(Protocol):
    def dispatch_next(self, worker_id: str) -> object | None: ...


class RecoveryDispatcherPort(Protocol):
    def dispatch_next(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class RuntimeStepResult:
    step: RuntimeStep
    disposition: HandlerDisposition
    observed_at: datetime
    dry_run: bool
    local_state_changed: bool
    venue_write_attempted: bool
    entry_eligible: bool
    result_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step.value,
            "disposition": self.disposition.value,
            "observed_at": _time_text(self.observed_at),
            "dry_run": self.dry_run,
            "local_state_changed": self.local_state_changed,
            "venue_write_attempted": self.venue_write_attempted,
            "entry_eligible": self.entry_eligible,
            "result_hash": self.result_hash,
        }


@dataclass(frozen=True, slots=True)
class ExecutorRuntimeStatus:
    runtime: ExecutorRuntimeReadModel
    daily_loss: DailyLossSnapshot
    work: ExecutionWorkScan
    startup_reconciled: bool
    shutdown_requested: bool
    active_started: bool
    entry_eligible: bool
    observed_at: datetime
    status_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime": self.runtime.as_dict(),
            "daily_loss": self.daily_loss.as_dict(),
            "work": self.work.as_dict(),
            "startup_reconciled": self.startup_reconciled,
            "shutdown_requested": self.shutdown_requested,
            "active_started": self.active_started,
            "entry_eligible": self.entry_eligible,
            "observed_at": _time_text(self.observed_at),
            "status_hash": self.status_hash,
        }


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    drain_limit: int
    drain_steps: tuple[RuntimeStepResult, ...]
    remaining_safety_work: int
    clean: bool
    released: bool
    observed_at: datetime
    report_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "drain_limit": self.drain_limit,
            "drain_steps": [item.as_dict() for item in self.drain_steps],
            "remaining_safety_work": self.remaining_safety_work,
            "clean": self.clean,
            "released": self.released,
            "observed_at": _time_text(self.observed_at),
            "report_hash": self.report_hash,
        }


@dataclass(frozen=True, slots=True)
class _LocalAssessment:
    loss: DailyLossSnapshot
    work: ExecutionWorkScan
    entry_eligible: bool
    loss_blocked: bool
    integrity_blocked: bool


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _time_text(value: datetime) -> str:
    return _utc(value, field="time").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _clock_read(clock: Clock) -> datetime:
    try:
        return _utc(clock(), field="runtime clock")
    except ValidationError:
        raise
    except Exception as error:
        raise RuntimeOrchestrationError("executor runtime clock failed") from error


def _handler_result(value: object, *, field: str) -> HandlerResult:
    if not isinstance(value, HandlerResult):
        raise TypeError(f"{field} must return HandlerResult")
    return value


def _result(
    *,
    step: RuntimeStep,
    disposition: HandlerDisposition,
    observed_at: datetime,
    dry_run: bool,
    local_state_changed: bool,
    venue_write_attempted: bool,
    entry_eligible: bool,
) -> RuntimeStepResult:
    material = {
        "step": step.value,
        "disposition": disposition.value,
        "observed_at": observed_at,
        "dry_run": dry_run,
        "local_state_changed": local_state_changed,
        "venue_write_attempted": venue_write_attempted,
        "entry_eligible": entry_eligible,
    }
    return RuntimeStepResult(
        step=step,
        disposition=disposition,
        observed_at=observed_at,
        dry_run=dry_run,
        local_state_changed=local_state_changed,
        venue_write_attempted=venue_write_attempted,
        entry_eligible=entry_eligible,
        result_hash=domain_hash("trading-harness/executor-runtime-step/v1", material),
    )


class ExecutorRuntime:
    """One-owner, one-step-at-a-time deterministic executor coordinator."""

    def __init__(
        self,
        *,
        runtime_store: ExecutorRuntimeStore,
        work_scanner: ExecutionWorkScanner,
        daily_loss: DailyLossLedger,
        instance_id: str,
        worker_id: str,
        startup_reconciler: StartupReconciliationHandler | None = None,
        recovery_reconciler: RecoveryReconciliationHandler | None = None,
        parent_reconciler: ParentReconciliationHandler | None = None,
        protection_inspector: ProtectionInspectionHandler | None = None,
        safety_handler: SafetyActionHandler | None = None,
        recovery_dispatcher: RecoveryDispatcherPort | None = None,
        entry_dispatcher: EntryDispatcherPort | None = None,
        clock: Clock | None = None,
        lease_seconds: int = 30,
    ) -> None:
        if type(runtime_store) is not ExecutorRuntimeStore:
            raise TypeError("runtime_store must be exact ExecutorRuntimeStore")
        if type(work_scanner) is not ExecutionWorkScanner:
            raise TypeError("work_scanner must be exact ExecutionWorkScanner")
        if type(daily_loss) is not DailyLossLedger:
            raise TypeError("daily_loss must be exact DailyLossLedger")
        for field, value in (("instance_id", instance_id), ("worker_id", worker_id)):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or any(ord(character) < 32 for character in value)
            ):
                raise ValidationError(f"{field} is invalid")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValidationError("lease_seconds must be from 5 through 300")
        runtime_status = runtime_store.read()
        if daily_loss.binding.config_hash != runtime_status.config_hash:
            raise ValidationError("daily-loss and runtime configuration hashes differ")
        self._runtime_store = runtime_store
        self._work_scanner = work_scanner
        self._daily_loss = daily_loss
        self._instance_id = instance_id
        self._worker_id = worker_id
        self._startup_reconciler = startup_reconciler
        self._recovery_reconciler = recovery_reconciler
        self._parent_reconciler = parent_reconciler
        self._protection_inspector = protection_inspector
        self._safety_handler = safety_handler
        self._recovery_dispatcher = recovery_dispatcher
        self._entry_dispatcher = entry_dispatcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_seconds = lease_seconds
        self._lease: RuntimeLease | None = None
        # A separate status process may infer that the currently fenced
        # runtime completed startup only from its persisted READY projection.
        # ``start()`` always resets this to false for a newly acquired lease.
        self._startup_reconciled = (
            runtime_status.lease_state is RuntimeLeaseState.ACTIVE
            and runtime_status.effective_risk_gate is ExecutorRiskGate.READY
        )
        self._shutdown_requested = False
        self._entry_submission_lock = threading.Lock()
        self._entry_submission_active = False

    @property
    def started(self) -> bool:
        return self._lease is not None

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def _now(self) -> datetime:
        return _clock_read(self._clock)

    @staticmethod
    def _port(value: object, method: str, field: str) -> None:
        if value is None or not callable(getattr(value, method, None)):
            raise RuntimeOrchestrationError(f"active runtime requires {field}")

    def _require_active_ports(self) -> None:
        for value, method, field in (
            (self._startup_reconciler, "reconcile_startup", "startup_reconciler"),
            (self._recovery_reconciler, "reconcile_next", "recovery_reconciler"),
            (self._parent_reconciler, "reconcile_next", "parent_reconciler"),
            (self._protection_inspector, "inspect_next", "protection_inspector"),
            (self._safety_handler, "act_next", "safety_handler"),
            (self._recovery_dispatcher, "dispatch_next", "recovery_dispatcher"),
            (self._entry_dispatcher, "dispatch_next", "entry_dispatcher"),
        ):
            self._port(value, method, field)

    def start(self) -> ExecutorRuntimeReadModel:
        """Acquire the runtime fence and enter running/reconciling, never READY."""

        if self._lease is not None:
            raise StateConflict("executor runtime is already started")
        self._require_active_ports()
        lease = self._runtime_store.acquire(
            instance_id=self._instance_id,
            lease_seconds=self._lease_seconds,
        )
        lease = self._runtime_store.heartbeat(
            instance_id=self._instance_id,
            fencing_token=lease.fencing_token,
            lease_seconds=self._lease_seconds,
        )
        lease = self._runtime_store.transition(
            instance_id=self._instance_id,
            fencing_token=lease.fencing_token,
            process_state=ExecutorProcessState.RUNNING,
            risk_gate=ExecutorRiskGate.RECONCILING,
        )
        self._lease = lease
        self._startup_reconciled = False
        self._shutdown_requested = False
        status = self._runtime_store.read()
        if status.effective_risk_gate is ExecutorRiskGate.READY:
            raise RuntimeIntegrityFailure("startup unexpectedly entered ready state")
        return status

    def request_shutdown(self) -> None:
        """Signal-safe intent flag; the serialized loop performs durable shutdown."""

        acquired = self._entry_submission_lock.acquire(blocking=False)
        try:
            self._shutdown_requested = True
            if acquired:
                self._entry_submission_active = False
        finally:
            if acquired:
                self._entry_submission_lock.release()

    @contextmanager
    def entry_submission_guard(self) -> Iterator[None]:
        """Hold the final revocable entry capability through one venue send.

        Entering this context is the point of no return for an already prepared
        entry. A shutdown or halt before entry rejects the send; one arriving
        after entry waits for the bounded one-shot transport to finish.
        """

        self._entry_submission_lock.acquire()
        try:
            status = self._runtime_store.read()
            if (
                not self._entry_submission_active
                or self._shutdown_requested
                or self._lease is None
                or not status.lease_current
                or not status.heartbeat_current
                or status.fencing_token != self._lease.fencing_token
                or status.instance_fingerprint
                != self._lease.instance_fingerprint
                or status.manual_halt
                or status.effective_risk_gate is not ExecutorRiskGate.READY
            ):
                raise EntrySubmissionRevoked(
                    "runtime entry submission capability is not active"
                )
            yield
        finally:
            self._entry_submission_lock.release()

    def _heartbeat(self) -> None:
        lease = self._lease
        if lease is None:
            raise RuntimeNotStarted("executor runtime has not acquired a lease")
        self._lease = self._runtime_store.heartbeat(
            instance_id=self._instance_id,
            fencing_token=lease.fencing_token,
            lease_seconds=self._lease_seconds,
        )

    def _halt_gate(self) -> bool:
        lease = self._lease
        if lease is None:
            return False
        status = self._runtime_store.read()
        if status.declared_risk_gate is ExecutorRiskGate.HALTED:
            return False
        self._lease = self._runtime_store.transition(
            instance_id=self._instance_id,
            fencing_token=lease.fencing_token,
            risk_gate=ExecutorRiskGate.HALTED,
        )
        return True

    def _engage_halt(self, reason: ManualHaltReason) -> None:
        acquired = self._entry_submission_lock.acquire(blocking=False)
        if acquired:
            self._entry_submission_active = False
            self._entry_submission_lock.release()
        try:
            self._runtime_store.engage_manual_halt(reason=reason)
        except (StateConflict, StorageError, ValidationError):
            # If even the halt projection cannot be written, the caller still
            # receives an integrity failure and must stop the process.
            pass

    def _assess_local(self) -> _LocalAssessment:
        try:
            if not self._runtime_store.verify_journal():
                raise StorageError("runtime journal verification returned false")
            loss = self._loss_snapshot()
            work = self._work_scanner.scan()
            self._validate_local_freshness(loss, work)
        except Exception as error:
            self._engage_halt(ManualHaltReason.INTERNAL_ERROR)
            raise RuntimeIntegrityFailure(
                "local executor integrity verification failed"
            ) from error
        integrity_blocked = not work.compatible
        loss_blocked = not loss.coverage_complete or loss.remaining <= 0
        if integrity_blocked or loss_blocked:
            self._halt_gate()
        return _LocalAssessment(
            loss=loss,
            work=work,
            entry_eligible=(
                work.compatible
                and loss.coverage_complete
                and loss.remaining > 0
                and not self._shutdown_requested
            ),
            loss_blocked=loss_blocked,
            integrity_blocked=integrity_blocked,
        )

    def _validate_local_freshness(
        self,
        loss: DailyLossSnapshot,
        work: ExecutionWorkScan,
    ) -> None:
        observed = self._now()
        runtime = self._runtime_store.read()
        maximum_age = timedelta(seconds=MAX_LOCAL_VIEW_AGE_SECONDS)
        values = [
            ("daily-loss snapshot", loss.as_of),
            ("execution work scan", work.observed_at),
        ]
        if runtime.lease_state is RuntimeLeaseState.ACTIVE:
            values.insert(0, ("runtime projection", runtime.observed_at))
        for field, value in values:
            if value > observed or observed - value > maximum_age:
                raise RuntimeIntegrityFailure(f"{field} is not current")

    @staticmethod
    def _kinds(work: ExecutionWorkScan) -> frozenset[ExecutionWorkKind]:
        return frozenset(item.kind for item in work.items)

    def _invoke(
        self,
        *,
        port: object,
        method: str,
        field: str,
        step: RuntimeStep,
        entry_eligible: bool,
    ) -> RuntimeStepResult:
        try:
            value = getattr(port, method)()
            result = _handler_result(value, field=field)
        except Exception as error:
            self._engage_halt(ManualHaltReason.INTERNAL_ERROR)
            raise RuntimeOrchestrationError(f"{field} failed closed") from error
        return _result(
            step=step,
            disposition=result.disposition,
            observed_at=self._now(),
            dry_run=False,
            local_state_changed=result.local_state_changed,
            venue_write_attempted=False,
            entry_eligible=entry_eligible,
        )

    def _dispatch_recovery(self) -> RuntimeStepResult:
        try:
            dispatched = self._recovery_dispatcher.dispatch_next()  # type: ignore[union-attr]
        except Exception as error:
            self._engage_halt(ManualHaltReason.INTERNAL_ERROR)
            raise RuntimeOrchestrationError(
                "recovery dispatcher failed closed"
            ) from error
        return _result(
            step=RuntimeStep.RECOVERY_DISPATCH,
            disposition=(
                HandlerDisposition.NO_WORK
                if dispatched is None
                else HandlerDisposition.PROGRESSED
            ),
            observed_at=self._now(),
            dry_run=False,
            local_state_changed=dispatched is not None,
            venue_write_attempted=dispatched is not None,
            entry_eligible=False,
        )

    def _dispatch_newly_prepared_recovery(
        self,
        safety: RuntimeStepResult,
        *,
        previously_visible: bool,
    ) -> RuntimeStepResult | None:
        if safety.disposition is HandlerDisposition.NO_WORK and previously_visible:
            return self._dispatch_recovery()
        if safety.disposition is not HandlerDisposition.PROGRESSED:
            return None
        refreshed = self._work_scanner.scan()
        self._validate_local_freshness(self._loss_snapshot(), refreshed)
        if not refreshed.compatible:
            raise RuntimeIntegrityFailure(
                "execution work became incompatible after safety action"
            )
        if ExecutionWorkKind.RECOVERY_DISPATCH in self._kinds(refreshed):
            return self._dispatch_recovery()
        return None

    def _run_priority(
        self,
        assessment: _LocalAssessment,
        *,
        allow_entry: bool,
        allow_startup: bool,
        drain: bool,
        entry_refresh_permitted: bool = False,
    ) -> RuntimeStepResult | None:
        kinds = self._kinds(assessment.work)
        entry_eligible = assessment.entry_eligible and allow_entry

        if assessment.integrity_blocked:
            return _result(
                step=RuntimeStep.INTEGRITY_BLOCKED,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )

        if ExecutionWorkKind.RECOVERY_RECONCILE in kinds:
            return self._invoke(
                port=self._recovery_reconciler,
                method="reconcile_next",
                field="recovery_reconciler",
                step=RuntimeStep.RECOVERY_RECONCILE,
                entry_eligible=False,
            )
        if ExecutionWorkKind.RECOVERY_IN_FLIGHT in kinds:
            return _result(
                step=RuntimeStep.RECOVERY_WAIT,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )

        pre_parent_safety = (
            ExecutionWorkKind.COMMAND_RECONCILE in kinds
            and ExecutionWorkKind.OPEN_INCIDENT in kinds
        )
        if pre_parent_safety:
            safety = self._invoke(
                port=self._safety_handler,
                method="act_next",
                field="safety_handler",
                step=RuntimeStep.SAFETY_ACTION,
                entry_eligible=False,
            )
            dispatched = self._dispatch_newly_prepared_recovery(
                safety,
                previously_visible=ExecutionWorkKind.RECOVERY_DISPATCH in kinds,
            )
            if dispatched is not None:
                return dispatched
            if safety.disposition is HandlerDisposition.PROGRESSED:
                return safety

        if ExecutionWorkKind.COMMAND_RECONCILE in kinds:
            return self._invoke(
                port=self._parent_reconciler,
                method="reconcile_next",
                field="parent_reconciler",
                step=RuntimeStep.PARENT_RECONCILE,
                entry_eligible=False,
            )
        if ExecutionWorkKind.COMMAND_IN_FLIGHT in kinds:
            return _result(
                step=RuntimeStep.PARENT_WAIT,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )

        protection_work = bool(
            kinds
            & {
                ExecutionWorkKind.PROTECTION_GAP,
                ExecutionWorkKind.OPEN_POSITION,
            }
        )
        safety_work = bool(
            kinds
            & {
                ExecutionWorkKind.PROTECTION_GAP,
                ExecutionWorkKind.OPEN_INCIDENT,
            }
        )
        if protection_work:
            inspected = self._invoke(
                port=self._protection_inspector,
                method="inspect_next",
                field="protection_inspector",
                step=RuntimeStep.PROTECTION_CHECK,
                entry_eligible=False,
            )
            if inspected.disposition is not HandlerDisposition.NO_WORK:
                return inspected
            if not safety_work:
                return _result(
                    step=RuntimeStep.PROTECTION_CHECK,
                    disposition=HandlerDisposition.WAITING,
                    observed_at=self._now(),
                    dry_run=False,
                    local_state_changed=False,
                    venue_write_attempted=False,
                    entry_eligible=False,
                )

        if safety_work:
            safety = self._invoke(
                port=self._safety_handler,
                method="act_next",
                field="safety_handler",
                step=RuntimeStep.SAFETY_ACTION,
                entry_eligible=False,
            )
            # A safety adapter may use this read-only pass to cache the exact
            # source snapshot for an already-queued recovery.  NO_WORK means
            # no additional authority was created and the serialized worker
            # may continue into the recovery dispatch lane in this same tick.
            # Any mutation, wait, or completion remains an observable step.
            dispatched = self._dispatch_newly_prepared_recovery(
                safety,
                previously_visible=ExecutionWorkKind.RECOVERY_DISPATCH in kinds,
            )
            if dispatched is not None:
                return dispatched
            if safety.disposition is not HandlerDisposition.NO_WORK:
                return safety

        if ExecutionWorkKind.RECOVERY_DISPATCH in kinds:
            return self._dispatch_recovery()

        if allow_startup and not self._startup_reconciled:
            startup = self._invoke(
                port=self._startup_reconciler,
                method="reconcile_startup",
                field="startup_reconciler",
                step=RuntimeStep.STARTUP_RECONCILE,
                entry_eligible=False,
            )
            if startup.disposition is HandlerDisposition.COMPLETE:
                self._startup_reconciled = True
            return startup

        if not drain and assessment.loss_blocked:
            return _result(
                step=RuntimeStep.LOSS_BLOCKED,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )

        if allow_entry and ExecutionWorkKind.COMMAND_DISPATCH in kinds:
            status = self._runtime_store.read()
            if status.effective_risk_gate is not ExecutorRiskGate.READY:
                return None
            if not entry_eligible or not entry_refresh_permitted:
                return _result(
                    step=(
                        RuntimeStep.INTEGRITY_BLOCKED
                        if assessment.integrity_blocked
                        else RuntimeStep.LOSS_BLOCKED
                    ),
                    disposition=HandlerDisposition.WAITING,
                    observed_at=self._now(),
                    dry_run=False,
                    local_state_changed=False,
                    venue_write_attempted=False,
                    entry_eligible=False,
                )
            if self._shutdown_requested:
                return _result(
                    step=RuntimeStep.IDLE,
                    disposition=HandlerDisposition.WAITING,
                    observed_at=self._now(),
                    dry_run=False,
                    local_state_changed=False,
                    venue_write_attempted=False,
                    entry_eligible=False,
                )
            with self._entry_submission_lock:
                if self._shutdown_requested:
                    return _result(
                        step=RuntimeStep.IDLE,
                        disposition=HandlerDisposition.WAITING,
                        observed_at=self._now(),
                        dry_run=False,
                        local_state_changed=False,
                        venue_write_attempted=False,
                        entry_eligible=False,
                    )
                self._entry_submission_active = True
            try:
                dispatched = self._entry_dispatcher.dispatch_next(  # type: ignore[union-attr]
                    self._worker_id
                )
            except Exception as error:
                self._engage_halt(ManualHaltReason.INTERNAL_ERROR)
                raise RuntimeOrchestrationError(
                    "entry dispatcher failed closed"
                ) from error
            finally:
                with self._entry_submission_lock:
                    self._entry_submission_active = False
            return _result(
                step=RuntimeStep.ENTRY_DISPATCH,
                disposition=(
                    HandlerDisposition.NO_WORK
                    if dispatched is None
                    else HandlerDisposition.PROGRESSED
                ),
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=dispatched is not None,
                venue_write_attempted=(
                    False
                    if dispatched is None
                    else bool(getattr(dispatched, "venue_write_attempted", True))
                ),
                entry_eligible=True,
            )
        return None

    def tick(self, *, entry_refresh_permitted: bool = False) -> RuntimeStepResult:
        """Run exactly one serialized active step."""

        if type(entry_refresh_permitted) is not bool:
            raise TypeError("entry_refresh_permitted must be boolean")
        if self._lease is None:
            raise RuntimeNotStarted("executor runtime has not started")
        if self._shutdown_requested:
            self._halt_gate()
            return _result(
                step=RuntimeStep.SHUTDOWN_DRAIN,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )
        self._heartbeat()
        assessment = self._assess_local()
        selected = self._run_priority(
            assessment,
            allow_entry=True,
            allow_startup=True,
            drain=False,
            entry_refresh_permitted=entry_refresh_permitted,
        )
        if selected is not None:
            return selected

        status = self._runtime_store.read()
        if not assessment.entry_eligible:
            return _result(
                step=(
                    RuntimeStep.INTEGRITY_BLOCKED
                    if assessment.integrity_blocked
                    else RuntimeStep.LOSS_BLOCKED
                ),
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )
        if status.manual_halt:
            return _result(
                step=RuntimeStep.LOSS_BLOCKED,
                disposition=HandlerDisposition.WAITING,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=False,
                venue_write_attempted=False,
                entry_eligible=False,
            )
        if status.declared_risk_gate is ExecutorRiskGate.HALTED:
            self._lease = self._runtime_store.transition(
                instance_id=self._instance_id,
                fencing_token=self._lease.fencing_token,
                risk_gate=ExecutorRiskGate.RECONCILING,
            )
            return _result(
                step=RuntimeStep.GATE_RECONCILING,
                disposition=HandlerDisposition.PROGRESSED,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=True,
                venue_write_attempted=False,
                entry_eligible=False,
            )
        if status.declared_risk_gate is ExecutorRiskGate.RECONCILING:
            self._lease = self._runtime_store.transition(
                instance_id=self._instance_id,
                fencing_token=self._lease.fencing_token,
                risk_gate=ExecutorRiskGate.READY,
            )
            return _result(
                step=RuntimeStep.GATE_READY,
                disposition=HandlerDisposition.PROGRESSED,
                observed_at=self._now(),
                dry_run=False,
                local_state_changed=True,
                venue_write_attempted=False,
                entry_eligible=True,
            )
        return _result(
            step=RuntimeStep.IDLE,
            disposition=HandlerDisposition.NO_WORK,
            observed_at=self._now(),
            dry_run=False,
            local_state_changed=False,
            venue_write_attempted=False,
            entry_eligible=True,
        )

    def _dry_step(self, assessment: _LocalAssessment) -> RuntimeStep:
        kinds = self._kinds(assessment.work)
        for selected, step in (
            (ExecutionWorkKind.RECOVERY_RECONCILE, RuntimeStep.RECOVERY_RECONCILE),
            (ExecutionWorkKind.RECOVERY_IN_FLIGHT, RuntimeStep.RECOVERY_WAIT),
            (ExecutionWorkKind.COMMAND_RECONCILE, RuntimeStep.PARENT_RECONCILE),
            (ExecutionWorkKind.COMMAND_IN_FLIGHT, RuntimeStep.PARENT_WAIT),
            (ExecutionWorkKind.PROTECTION_GAP, RuntimeStep.PROTECTION_CHECK),
            (ExecutionWorkKind.OPEN_POSITION, RuntimeStep.PROTECTION_CHECK),
            (ExecutionWorkKind.OPEN_INCIDENT, RuntimeStep.SAFETY_ACTION),
            (ExecutionWorkKind.RECOVERY_DISPATCH, RuntimeStep.RECOVERY_DISPATCH),
        ):
            if selected in kinds:
                return step
        if assessment.integrity_blocked:
            return RuntimeStep.INTEGRITY_BLOCKED
        if not self._startup_reconciled:
            return RuntimeStep.STARTUP_RECONCILE
        if assessment.loss_blocked:
            return RuntimeStep.LOSS_BLOCKED
        if ExecutionWorkKind.COMMAND_DISPATCH in kinds:
            return RuntimeStep.ENTRY_DISPATCH
        return RuntimeStep.IDLE

    def dry_run(self) -> RuntimeStepResult:
        """Inspect the next lane without calling any injected port or mutating runtime."""

        assessment = self._assess_local_read_only()
        step = self._dry_step(assessment)
        runtime = self._runtime_store.read()
        if runtime.manual_halt and step in {
            RuntimeStep.IDLE,
            RuntimeStep.ENTRY_DISPATCH,
            RuntimeStep.GATE_RECONCILING,
            RuntimeStep.GATE_READY,
        }:
            step = RuntimeStep.LOSS_BLOCKED
        elif (
            self._startup_reconciled
            and assessment.entry_eligible
            and step in {RuntimeStep.IDLE, RuntimeStep.ENTRY_DISPATCH}
        ):
            if runtime.declared_risk_gate is ExecutorRiskGate.HALTED:
                step = RuntimeStep.GATE_RECONCILING
            elif runtime.declared_risk_gate is ExecutorRiskGate.RECONCILING:
                step = RuntimeStep.GATE_READY
        return _result(
            step=step,
            disposition=HandlerDisposition.WAITING,
            observed_at=self._now(),
            dry_run=True,
            local_state_changed=False,
            venue_write_attempted=False,
            entry_eligible=assessment.entry_eligible,
        )

    def _assess_local_read_only(self) -> _LocalAssessment:
        try:
            if not self._runtime_store.verify_journal():
                raise StorageError("runtime journal verification returned false")
            loss = self._loss_snapshot()
            work = self._work_scanner.scan()
            self._validate_local_freshness(loss, work)
        except Exception as error:
            raise RuntimeIntegrityFailure(
                "read-only executor integrity verification failed"
            ) from error
        integrity_blocked = not work.compatible
        loss_blocked = not loss.coverage_complete or loss.remaining <= 0
        return _LocalAssessment(
            loss=loss,
            work=work,
            entry_eligible=(
                work.compatible
                and loss.coverage_complete
                and loss.remaining > 0
                and not self._shutdown_requested
            ),
            loss_blocked=loss_blocked,
            integrity_blocked=integrity_blocked,
        )

    def _loss_snapshot(self) -> DailyLossSnapshot:
        """Use the newest exact common venue watermark, never a rounded gap."""

        try:
            return self._daily_loss.latest_complete_snapshot(
                maximum_age_seconds=MAX_LOCAL_VIEW_AGE_SECONDS
            )
        except StateConflict:
            # Preserve an explicit incomplete current projection so the risk
            # gate can report LOSS_BLOCKED rather than misclassifying missing
            # or stale source coverage as a corrupt local store.
            return self._daily_loss.snapshot(require_complete=False)

    def status(self) -> ExecutorRuntimeStatus:
        """Return local verified status without touching any injected port."""

        observed = self._now()
        assessment = self._assess_local_read_only()
        runtime = self._runtime_store.read()
        entry_eligible = (
            assessment.entry_eligible
            and self._startup_reconciled
            and runtime.effective_risk_gate is ExecutorRiskGate.READY
            and not runtime.manual_halt
        )
        material = {
            "runtime_read_model_hash": runtime.read_model_hash,
            "daily_loss_snapshot_hash": assessment.loss.snapshot_hash,
            "work_scan_hash": assessment.work.scan_hash,
            "startup_reconciled": self._startup_reconciled,
            "shutdown_requested": self._shutdown_requested,
            "active_started": self.started,
            "entry_eligible": entry_eligible,
            "observed_at": observed,
        }
        return ExecutorRuntimeStatus(
            runtime=runtime,
            daily_loss=assessment.loss,
            work=assessment.work,
            startup_reconciled=self._startup_reconciled,
            shutdown_requested=self._shutdown_requested,
            active_started=self.started,
            entry_eligible=entry_eligible,
            observed_at=observed,
            status_hash=domain_hash(
                "trading-harness/executor-runtime-status/v1", material
            ),
        )

    @staticmethod
    def _remaining_safety_work(work: ExecutionWorkScan) -> int:
        draining = {
            ExecutionWorkKind.RECOVERY_RECONCILE,
            ExecutionWorkKind.RECOVERY_IN_FLIGHT,
            ExecutionWorkKind.COMMAND_RECONCILE,
            ExecutionWorkKind.COMMAND_IN_FLIGHT,
            ExecutionWorkKind.PROTECTION_GAP,
            ExecutionWorkKind.OPEN_POSITION,
            ExecutionWorkKind.OPEN_INCIDENT,
            ExecutionWorkKind.RECOVERY_DISPATCH,
        }
        return sum(item.kind in draining for item in work.items)

    def shutdown(self, *, max_drain_steps: int = 20) -> ShutdownReport:
        """Drain bounded safety work, then stop and release the runtime fence."""

        if type(max_drain_steps) is not int or not 0 <= max_drain_steps <= 10_000:
            raise ValidationError("max_drain_steps must be from 0 through 10000")
        lease = self._lease
        if lease is None:
            raise RuntimeNotStarted("executor runtime has not started")
        self.request_shutdown()
        self._halt_gate()
        steps: list[RuntimeStepResult] = []
        drain_failed = False
        for _ in range(max_drain_steps):
            try:
                self._heartbeat()
                assessment = self._assess_local()
            except RuntimeOrchestrationError:
                drain_failed = True
                break
            if assessment.integrity_blocked:
                break
            if self._remaining_safety_work(assessment.work) == 0:
                break
            try:
                selected = self._run_priority(
                    assessment,
                    allow_entry=False,
                    allow_startup=False,
                    drain=True,
                )
            except RuntimeOrchestrationError:
                drain_failed = True
                break
            if selected is None:
                break
            steps.append(selected)

        try:
            final_assessment = self._assess_local_read_only()
            remaining = self._remaining_safety_work(final_assessment.work)
            final_compatible = final_assessment.work.compatible
        except RuntimeIntegrityFailure:
            remaining = 1
            final_compatible = False
            drain_failed = True
        self._heartbeat()
        self._lease = self._runtime_store.request_stop(
            instance_id=self._instance_id,
            fencing_token=self._lease.fencing_token,
        )
        self._lease = self._runtime_store.mark_stopped(
            instance_id=self._instance_id,
            fencing_token=self._lease.fencing_token,
        )
        self._lease = self._runtime_store.release(
            instance_id=self._instance_id,
            fencing_token=self._lease.fencing_token,
        )
        released_status = self._runtime_store.read()
        released = released_status.lease_state is RuntimeLeaseState.RELEASED
        observed = self._now()
        material = {
            "drain_limit": max_drain_steps,
            "drain_step_hashes": tuple(item.result_hash for item in steps),
            "remaining_safety_work": remaining,
            "clean": remaining == 0 and final_compatible and not drain_failed,
            "released": released,
            "observed_at": observed,
        }
        report = ShutdownReport(
            drain_limit=max_drain_steps,
            drain_steps=tuple(steps),
            remaining_safety_work=remaining,
            clean=remaining == 0 and final_compatible and not drain_failed,
            released=released,
            observed_at=observed,
            report_hash=domain_hash(
                "trading-harness/executor-runtime-shutdown/v1", material
            ),
        )
        self._lease = None
        return report


__all__ = (
    "MAX_LOCAL_VIEW_AGE_SECONDS",
    "EntryDispatcherPort",
    "ExecutorRuntime",
    "ExecutorRuntimeStatus",
    "HandlerDisposition",
    "HandlerResult",
    "ParentReconciliationHandler",
    "ProtectionInspectionHandler",
    "RecoveryDispatcherPort",
    "RecoveryReconciliationHandler",
    "RuntimeIntegrityFailure",
    "RuntimeNotStarted",
    "RuntimeOrchestrationError",
    "RuntimeStep",
    "RuntimeStepResult",
    "SafetyActionHandler",
    "ShutdownReport",
    "StartupReconciliationHandler",
)
