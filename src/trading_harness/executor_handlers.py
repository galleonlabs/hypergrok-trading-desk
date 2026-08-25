"""Narrow TESTNET adapters for :mod:`trading_harness.executor_runtime`.

The runtime deliberately depends on tiny handler protocols.  This module is
the capital-boundary adapter that joins those protocols to the already
reviewed, read-only venue reconcilers and the deterministic account-safety
controller.  It has no credential provider, signer, dispatcher, or venue
write transport.

Every public handler invocation selects at most one durable command, recovery,
or protection record.  Account evidence is fetched afresh for that invocation
and must be an exact, allowlisted TESTNET snapshot.  Startup reaches
``COMPLETE`` only after the durable reconciliation lanes are empty and the
same fresh snapshot passes both protection inspection and the safety scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import threading
from typing import Any, TypeAlias

from .account_safety_controller import (
    PreparedRecovery,
    SafetyControllerResult,
    SafetyControllerState,
    TestnetAccountSafetyController,
)
from .canonical import canonical_decimal, domain_hash
from .domain import Environment
from .errors import StateConflict, StorageError, ValidationError
from .execution_store import (
    AttemptRecord,
    CommandRecord,
    EventRecord,
    ExecutionStore,
    IncidentRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryAttempt,
    RecoveryCommand,
    RecoveryOutbox,
)
from .executor_runtime import HandlerDisposition, HandlerResult
from .hyperliquid_account import HyperliquidAccountSnapshot
from .hyperliquid_recovery_reader import HyperliquidRecoveryVenueReader
from .hyperliquid_wire import HyperliquidNetwork
from .market_data import public_info_endpoint
from .reconciliation_coordinator import (
    HyperliquidVenueReconciler,
    MainEntryReconciliationCoordinator,
    MainReconciliationResult,
    _verify_snapshot_hash,
)
from .recovery_reconciliation import (
    RecoveryCoordinationResult,
    RecoveryReconciliationCoordinator,
)


Clock: TypeAlias = Callable[[], datetime]
AccountSnapshotReader: TypeAlias = Callable[[str, str], HyperliquidAccountSnapshot]
MarketBriefReader: TypeAlias = Callable[[str, str], Mapping[str, Any]]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SNAPSHOT_AGE_MS = 5_000
_MAX_FUTURE_SKEW_MS = 5_000
_COMMAND_STATES = frozenset(
    {"queued", "claimed", "submitted_unknown", "reconciling", "terminal"}
)
_RECOVERY_STATES = frozenset(
    {
        "queued",
        "claimed",
        "signing",
        "submitted_unknown",
        "reconciling",
        "terminal",
    }
)
_MAIN_RECONCILIATION_STATES = frozenset({"submitted_unknown", "reconciling"})
_RECOVERY_RECONCILIATION_STATES = frozenset(
    {"submitted_unknown", "reconciling"}
)


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _clock_read(clock: Clock) -> datetime:
    try:
        return _utc(clock(), field="executor handler clock")
    except ValidationError:
        raise
    except Exception as error:
        raise StorageError("executor handler clock failed") from error


def _milliseconds(value: datetime) -> int:
    delta = _utc(value, field="time") - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError("time predates Unix epoch")
    return result


def _datetime_ms(value: int) -> datetime:
    if type(value) is not int or value < 0:
        raise ValidationError("millisecond timestamp is invalid")
    return _EPOCH + timedelta(milliseconds=value)


def _exact_records(
    value: object,
    expected: type[Any],
    *,
    field: str,
) -> tuple[Any, ...]:
    if not isinstance(value, tuple) or any(type(item) is not expected for item in value):
        raise StorageError(f"{field} did not return exact durable records")
    return value


def _unique(records: tuple[Any, ...], key: str, *, field: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        identifier = getattr(record, key, None)
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise StorageError(f"{field} contains a duplicate or invalid {key}")
        result[identifier] = record
    return result


def _command_view(
    store: ExecutionStore,
) -> tuple[dict[str, CommandRecord], dict[str, OutboxRecord]]:
    commands = _unique(
        _exact_records(store.list_commands(), CommandRecord, field="commands"),
        "command_id",
        field="commands",
    )
    outboxes = _unique(
        _exact_records(store.list_outboxes(), OutboxRecord, field="outboxes"),
        "command_id",
        field="outboxes",
    )
    if set(commands) != set(outboxes):
        raise StorageError("command and outbox durable views disagree")
    for command_id, command in commands.items():
        outbox = outboxes[command_id]
        if (
            command.state not in _COMMAND_STATES
            or outbox.state not in _COMMAND_STATES
            or command.state != outbox.state
        ):
            raise StorageError("command and outbox states disagree")
    return commands, outboxes


def _recovery_view(
    store: ExecutionStore,
) -> tuple[dict[str, RecoveryCommand], dict[str, RecoveryOutbox]]:
    commands = _unique(
        _exact_records(
            store.list_recovery_commands(),
            RecoveryCommand,
            field="recovery commands",
        ),
        "recovery_command_id",
        field="recovery commands",
    )
    outboxes = _unique(
        _exact_records(
            store.list_recovery_outboxes(),
            RecoveryOutbox,
            field="recovery outboxes",
        ),
        "recovery_command_id",
        field="recovery outboxes",
    )
    if set(commands) != set(outboxes):
        raise StorageError("recovery command and outbox durable views disagree")
    for command_id, command in commands.items():
        outbox = outboxes[command_id]
        if (
            command.state not in _RECOVERY_STATES
            or outbox.state not in _RECOVERY_STATES
            or command.state != outbox.state
            or type(command.priority) is not int
            or not 0 <= command.priority <= 2
        ):
            raise StorageError("recovery command and outbox states disagree")
    return commands, outboxes


def _event_tip(store: ExecutionStore) -> tuple[int, str | None]:
    events = _exact_records(store.list_events(), EventRecord, field="events")
    previous: EventRecord | None = None
    for event in events:
        if previous is not None and (
            event.event_sequence != previous.event_sequence + 1
            or event.previous_hash != previous.event_hash
        ):
            raise StorageError("execution event chain is discontinuous")
        if previous is None and event.previous_hash is not None:
            raise StorageError("execution event chain has an invalid root")
        previous = event
    return (0, None) if previous is None else (previous.event_sequence, previous.event_hash)


def _changed(before: tuple[int, str | None], after: tuple[int, str | None]) -> bool:
    if after == before:
        return False
    if after[0] <= before[0]:
        raise StorageError("execution event chain moved backwards")
    return True


def _handler_from_safety(
    result: SafetyControllerResult,
    *,
    local_state_changed: bool,
    startup: bool,
    recovery_ready: bool,
) -> HandlerResult:
    if type(result) is not SafetyControllerResult:
        raise TypeError("safety controller must return exact SafetyControllerResult")
    if local_state_changed:
        return HandlerResult(HandlerDisposition.PROGRESSED, True)
    if result.state is SafetyControllerState.SAFE:
        return HandlerResult(
            HandlerDisposition.COMPLETE if startup else HandlerDisposition.NO_WORK,
            False,
        )
    if result.state is SafetyControllerState.ACTIVE and recovery_ready:
        # ``ExecutorRuntime`` deliberately falls through from this NO_WORK to
        # the recovery dispatcher.  The dispatcher consumes the cached exact
        # preparation through ``TestnetSafetyActionHandler.prepare``.
        return HandlerResult(HandlerDisposition.NO_WORK, False)
    return HandlerResult(HandlerDisposition.WAITING, False)


@dataclass(frozen=True, slots=True)
class _TestnetAccountContext:
    store: ExecutionStore
    safety_controller: TestnetAccountSafetyController
    account_reader: AccountSnapshotReader
    market_brief_reader: MarketBriefReader | None
    clock: Clock

    def __post_init__(self) -> None:
        if type(self.store) is not ExecutionStore:
            raise TypeError("store must be the exact ExecutionStore implementation")
        if self.store.environment is not Environment.TESTNET:
            raise ValidationError("executor handlers are TESTNET-only")
        if type(self.safety_controller) is not TestnetAccountSafetyController:
            raise TypeError(
                "safety_controller must be exact TestnetAccountSafetyController"
            )
        if self.safety_controller.store is not self.store:
            raise StateConflict("safety controller is bound to another store")
        if not callable(self.account_reader):
            raise TypeError("account_reader must be callable")
        if self.market_brief_reader is not None and not callable(
            self.market_brief_reader
        ):
            raise TypeError("market_brief_reader must be callable or None")
        if not callable(self.clock):
            raise TypeError("clock must be callable")

    @property
    def main_account_address(self) -> str:
        value = getattr(self.safety_controller.signing_account, "main_account_address", None)
        if not isinstance(value, str) or not value:
            raise StateConflict("safety controller lacks its bound main account")
        return value

    def snapshot(self) -> HyperliquidAccountSnapshot:
        value = self.account_reader(self.main_account_address, "testnet")
        if type(value) is not HyperliquidAccountSnapshot:
            raise TypeError("account_reader must return exact HyperliquidAccountSnapshot")
        now_ms = _milliseconds(_clock_read(self.clock))
        _verify_snapshot_hash(value)
        if (
            value.network != "testnet"
            or value.source_url != public_info_endpoint("testnet")
            or value.main_account_address != self.main_account_address
        ):
            raise StateConflict("account snapshot is outside the TESTNET handler scope")
        if (
            value.received_at_ms < value.server_time_ms
            or value.age_ms != value.received_at_ms - value.server_time_ms
            or value.received_at_ms > now_ms + _MAX_FUTURE_SKEW_MS
        ):
            raise StateConflict("account snapshot provenance is invalid")
        age_ms = now_ms - value.server_time_ms
        if age_ms > _MAX_SNAPSHOT_AGE_MS or age_ms < -_MAX_FUTURE_SKEW_MS:
            raise StateConflict("account snapshot is stale or future-dated")
        return value

    def market_brief(
        self, snapshot: HyperliquidAccountSnapshot
    ) -> Mapping[str, Any] | None:
        if len(snapshot.positions) != 1 or self.market_brief_reader is None:
            return None
        value = self.market_brief_reader(snapshot.positions[0].symbol, "testnet")
        if not isinstance(value, Mapping):
            raise TypeError("market_brief_reader must return a mapping")
        return value


class TestnetParentReconciliationHandler:
    """Reconcile one exact submitted parent command using fresh venue truth."""

    def __init__(
        self,
        context: _TestnetAccountContext,
        coordinator: MainEntryReconciliationCoordinator,
        venue_reconciler: HyperliquidVenueReconciler,
        *,
        worker_id: str,
        lease_seconds: int = 15,
    ) -> None:
        if type(context) is not _TestnetAccountContext:
            raise TypeError("context must be the exact TESTNET account context")
        if type(coordinator) is not MainEntryReconciliationCoordinator:
            raise TypeError("coordinator must be exact MainEntryReconciliationCoordinator")
        if coordinator.store is not context.store:
            raise StateConflict("main reconciliation coordinator uses another store")
        if coordinator.network is not HyperliquidNetwork.TESTNET:
            raise ValidationError("main reconciliation coordinator must use TESTNET")
        if type(venue_reconciler) is not HyperliquidVenueReconciler:
            raise TypeError("venue_reconciler must be exact HyperliquidVenueReconciler")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or worker_id != worker_id.strip()
            or len(worker_id) > 128
        ):
            raise ValidationError("worker_id is invalid")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 60:
            raise ValidationError("lease_seconds must be from 5 through 60")
        self._context = context
        self._coordinator = coordinator
        self._venue_reconciler = venue_reconciler
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def reconcile_next(self) -> HandlerResult:
        commands, outboxes = _command_view(self._context.store)
        now = _clock_read(self._context.clock)
        candidates = tuple(
            sorted(
                (
                    command
                    for command in commands.values()
                    if command.state in _MAIN_RECONCILIATION_STATES
                    or (
                        command.state == "claimed"
                        and outboxes[command.command_id].current_attempt_id is not None
                        and outboxes[command.command_id].lease_expires_at is not None
                        and outboxes[command.command_id].lease_expires_at <= now
                    )
                ),
                key=lambda item: (item.updated_at, item.command_id),
            )
        )
        if not candidates:
            return HandlerResult(HandlerDisposition.NO_WORK, False)
        command = candidates[0]
        outbox = outboxes[command.command_id]
        attempt = self._context.store.get_attempt(command.command_id)
        if type(attempt) is not AttemptRecord:
            raise StorageError("parent attempt lookup did not return an exact record")
        expired_prepared = (
            command.state == "claimed"
            and outbox.lease_expires_at is not None
            and outbox.lease_expires_at <= now
        )
        if (
            attempt.command_id != command.command_id
            or outbox.current_attempt_id != attempt.attempt_id
            or attempt.state
            not in (
                {"prepared", "unknown", "response_received"}
                if expired_prepared
                else {"unknown", "response_received"}
            )
        ):
            raise StateConflict("parent reconciliation attempt binding is invalid")
        claim_at = _clock_read(self._context.clock)
        if expired_prepared:
            self._context.store.normalize_expired_claims(at=claim_at)
            command = self._context.store.get_command(command.command_id)
            outbox = self._context.store.get_outbox(command.command_id)
            attempt = self._context.store.get_attempt(command.command_id)
            if (
                command.state != "submitted_unknown"
                or outbox.state != "submitted_unknown"
                or attempt.state != "unknown"
            ):
                raise StateConflict("expired parent attempt did not normalize to unknown")
        snapshot = self._context.snapshot()
        preflight = self._context.store.get_preflight(command.command_id)
        if preflight.account_server_time_ms is None:
            raise StateConflict(
                "parent reconciliation lacks venue-server fill watermark"
            )
        bundle = self._coordinator.read_bundle(
            self._venue_reconciler,
            snapshot,
            command_id=command.command_id,
            fills_start_time_ms=preflight.account_server_time_ms,
            fills_end_time_ms=snapshot.server_time_ms,
        )
        before = _event_tip(self._context.store)
        claim_at = _clock_read(self._context.clock)
        claim = self._context.store.claim_reconciliation(
            command.command_id,
            self._worker_id,
            at=claim_at,
            lease_seconds=self._lease_seconds,
        )
        if type(claim) is not OutboxRecord:
            raise StorageError("parent reconciliation claim is not an exact outbox")
        if (
            claim.command_id != command.command_id
            or claim.worker_id != self._worker_id
            or claim.state != "reconciling"
        ):
            raise StateConflict("parent reconciliation claim binding is invalid")
        events = _event_tip(self._context.store)
        reconciliation_id = domain_hash(
            "trading-harness/executor-parent-handler/v1",
            {
                "command_id": command.command_id,
                "attempt_id": attempt.attempt_id,
                "fencing_token": claim.fencing_token,
                "account_snapshot_hash": snapshot.snapshot_hash,
                "event_tip": events[1],
            },
        )
        try:
            result = self._coordinator.apply_bundle(
                bundle,
                snapshot,
                worker_id=self._worker_id,
                fencing_token=claim.fencing_token,
                reconciliation_id=reconciliation_id,
            )
        except Exception as error:
            current_claim = self._context.store.get_outbox(command.command_id)
            if (
                current_claim.state == "reconciling"
                and current_claim.worker_id == self._worker_id
                and current_claim.fencing_token == claim.fencing_token
            ):
                self._context.store.release_reconciliation_claim(
                    command.command_id,
                    self._worker_id,
                    claim.fencing_token,
                    at=_clock_read(self._context.clock),
                    reason="apply_failed:" + type(error).__name__,
                )
            raise
        if type(result) is not MainReconciliationResult or result.command_id != command.command_id:
            raise StorageError("main coordinator returned an invalid reconciliation result")
        if not _changed(before, _event_tip(self._context.store)):
            raise StorageError("main reconciliation reported no durable transition")
        return HandlerResult(HandlerDisposition.PROGRESSED, True)


class TestnetRecoveryReconciliationHandler:
    """Reconcile one exact submitted safety recovery using fresh venue truth."""

    def __init__(
        self,
        context: _TestnetAccountContext,
        coordinator: RecoveryReconciliationCoordinator,
        venue_reader: HyperliquidRecoveryVenueReader,
        *,
        worker_id: str,
    ) -> None:
        if type(context) is not _TestnetAccountContext:
            raise TypeError("context must be the exact TESTNET account context")
        if type(coordinator) is not RecoveryReconciliationCoordinator:
            raise TypeError(
                "coordinator must be exact RecoveryReconciliationCoordinator"
            )
        if type(venue_reader) is not HyperliquidRecoveryVenueReader:
            raise TypeError("venue_reader must be exact HyperliquidRecoveryVenueReader")
        if coordinator.store is not context.store or venue_reader.store is not context.store:
            raise StateConflict("recovery reconciliation components use another store")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or worker_id != worker_id.strip()
            or len(worker_id) > 128
        ):
            raise ValidationError("worker_id is invalid")
        self._context = context
        self._coordinator = coordinator
        self._venue_reader = venue_reader
        self._worker_id = worker_id

    def reconcile_next(self) -> HandlerResult:
        commands, outboxes = _recovery_view(self._context.store)
        now = _clock_read(self._context.clock)
        candidates = tuple(
            sorted(
                (
                    command
                    for command in commands.values()
                    if command.state in _RECOVERY_RECONCILIATION_STATES
                    or (
                        command.state in {"claimed", "signing"}
                        and outboxes[
                            command.recovery_command_id
                        ].current_attempt_id is not None
                        and outboxes[
                            command.recovery_command_id
                        ].lease_expires_at is not None
                        and outboxes[
                            command.recovery_command_id
                        ].lease_expires_at <= now
                    )
                ),
                key=lambda item: (item.priority, item.updated_at, item.recovery_command_id),
            )
        )
        if not candidates:
            return HandlerResult(HandlerDisposition.NO_WORK, False)
        command = candidates[0]
        outbox = outboxes[command.recovery_command_id]
        attempt = self._context.store.get_recovery_attempt(
            command.recovery_command_id
        )
        if type(attempt) is not RecoveryAttempt:
            raise StorageError("recovery attempt lookup did not return an exact record")
        expired_prepared = (
            command.state in {"claimed", "signing"}
            and outbox.lease_expires_at is not None
            and outbox.lease_expires_at <= now
        )
        if (
            attempt.recovery_command_id != command.recovery_command_id
            or outbox.current_attempt_id != attempt.attempt_id
            or attempt.state
            not in (
                {"prepared", "sending", "unknown", "response_received"}
                if expired_prepared
                else {"unknown", "response_received"}
            )
        ):
            raise StateConflict("recovery reconciliation attempt binding is invalid")
        before = _event_tip(self._context.store)
        if expired_prepared:
            self._context.store.normalize_expired_claims(at=now)
            command = self._context.store.get_recovery_command(
                command.recovery_command_id
            )
            outbox = self._context.store.get_recovery_outbox(
                command.recovery_command_id
            )
            attempt = self._context.store.get_recovery_attempt(
                command.recovery_command_id
            )
            if (
                command.state != "submitted_unknown"
                or outbox.state != "submitted_unknown"
                or attempt.state != "unknown"
            ):
                raise StateConflict("expired recovery attempt did not normalize to unknown")
        snapshot = self._context.snapshot()
        try:
            recovery_material = json.loads(command.recovery_material_json)
        except ValueError as error:
            raise StateConflict("recovery material is invalid JSON") from error
        if not isinstance(recovery_material, dict):
            raise StateConflict("recovery material is not an object")
        if command.kind == "reduce_only_close":
            fill_start_time_ms = recovery_material.get(
                "position_snapshot_time_ms"
            )
        elif command.kind == "cancel_by_cloid":
            fill_start_time_ms = recovery_material.get(
                "account_snapshot_time_ms"
            )
        else:
            parent_preflight = self._context.store.get_preflight(
                command.parent_command_id
            )
            fill_start_time_ms = parent_preflight.account_server_time_ms
        if type(fill_start_time_ms) is not int or fill_start_time_ms < 0:
            raise StateConflict(
                "recovery reconciliation lacks venue-server fill watermark"
            )
        evidence = self._venue_reader.read(
            command,
            snapshot,
            fills_start_time_ms=fill_start_time_ms,
            fills_end_time_ms=snapshot.server_time_ms,
        )
        result = self._coordinator.reconcile(
            command.recovery_command_id,
            self._worker_id,
            snapshot=snapshot,
            evidence=evidence,
            at=evidence.observed_at,
        )
        if (
            type(result) is not RecoveryCoordinationResult
            or result.recovery_command_id != command.recovery_command_id
        ):
            raise StorageError("recovery coordinator returned an invalid result")
        if not _changed(before, _event_tip(self._context.store)):
            raise StorageError("recovery reconciliation reported no durable transition")
        if "evidence_stale_after_claim" in result.incomplete_reasons:
            return HandlerResult(HandlerDisposition.WAITING, True)
        return HandlerResult(HandlerDisposition.PROGRESSED, True)


class TestnetProtectionInspectionHandler:
    """Inspect one durable position/protection pair against a fresh snapshot."""

    def __init__(self, context: _TestnetAccountContext) -> None:
        if type(context) is not _TestnetAccountContext:
            raise TypeError("context must be the exact TESTNET account context")
        self._context = context

    def _instrument_symbol(
        self,
        command: CommandRecord,
        protection: ProtectionRecord,
        snapshot: HyperliquidAccountSnapshot,
    ) -> str:
        plan = self._context.store.get_plan_payload(command.plan_hash)
        if not isinstance(plan, Mapping):
            raise StorageError("durable plan payload is not a mapping")
        entry = plan.get("entry")
        if not isinstance(entry, Mapping):
            raise StorageError("durable plan lacks its entry mapping")
        instrument = entry.get("instrument")
        if (
            entry.get("environment") != "testnet"
            or entry.get("venue") != "hyperliquid"
            or entry.get("account_id") != self._context.store.account_id
            or instrument != protection.instrument
            or not isinstance(instrument, str)
            or not instrument.endswith("-PERP")
        ):
            raise StateConflict("durable protection plan is outside handler scope")
        symbol = instrument[:-5]
        if not symbol or snapshot.metadata.instrument(symbol).symbol != symbol:
            raise StateConflict("durable protection instrument is absent from metadata")
        return symbol

    def _inspect_snapshot(self, snapshot: HyperliquidAccountSnapshot) -> HandlerResult:
        commands, _ = _command_view(self._context.store)
        positions = _exact_records(
            self._context.store.list_positions(),
            PositionRecord,
            field="positions",
        )
        protections = _exact_records(
            self._context.store.list_protections(),
            ProtectionRecord,
            field="protections",
        )
        _unique(positions, "instrument", field="positions")
        protection_map = _unique(protections, "command_id", field="protections")
        candidates = tuple(
            sorted(
                (item for item in positions if item.signed_quantity != Decimal("0")),
                key=lambda item: (item.observed_at, item.instrument),
            )
        )
        if not candidates:
            return HandlerResult(HandlerDisposition.NO_WORK, False)
        position = candidates[0]
        matching = tuple(
            item for item in protections if item.instrument == position.instrument
        )
        if len(matching) != 1:
            raise StorageError("open durable position lacks one exact protection record")
        protection = matching[0]
        if protection_map.get(protection.command_id) != protection:
            raise StorageError("durable protection identity is inconsistent")
        command = commands.get(protection.command_id)
        if command is None:
            raise StorageError("durable protection references an unknown command")
        symbol = self._instrument_symbol(command, protection, snapshot)
        if position.observed_at > _datetime_ms(snapshot.server_time_ms):
            raise StateConflict("fresh account snapshot predates durable position truth")

        venue_position = snapshot.position(symbol)
        code: str | None = None
        covered = Decimal("0")
        venue_quantity = Decimal("0") if venue_position is None else venue_position.signed_size
        if venue_position is None:
            code = "POSITION_ACCOUNT_DIVERGENCE"
        elif venue_quantity != position.signed_quantity:
            if (venue_quantity > 0) != (position.signed_quantity > 0):
                code = "POSITION_DIRECTION_CONTRADICTION"
            else:
                code = "POSITION_ACCOUNT_DIVERGENCE"
        else:
            coverage = snapshot.protection_coverage(
                symbol,
                expected_stop_cloids=(protection.stop_cloid,),
            )
            covered = coverage.covered_size
            required = venue_position.absolute_size
            if covered == required:
                return HandlerResult(HandlerDisposition.NO_WORK, False)
            if covered == 0:
                code = "PROTECTION_FAILED"
            elif covered < required:
                code = "POSITION_UNDER_PROTECTED"
            else:
                code = "POSITION_OVER_PROTECTED"

        assert code is not None
        incidents = _exact_records(
            self._context.store.list_incidents(command.command_id),
            IncidentRecord,
            field="command incidents",
        )
        if any(
            item.command_id == command.command_id
            and item.code == code
            and item.severity == "critical"
            and item.state == "open"
            for item in incidents
        ):
            # The existing durable incident is the one safety authority must
            # consume.  Re-opening a per-snapshot duplicate here would starve
            # the serialized safety lane.
            return HandlerResult(HandlerDisposition.NO_WORK, False)
        incident_id = domain_hash(
            "trading-harness/executor-protection-inspection/v1",
            {
                "command_id": command.command_id,
                "code": code,
                "account_snapshot_hash": snapshot.snapshot_hash,
            },
        )
        before = _event_tip(self._context.store)
        self._context.store.record_incident(
            incident_id=incident_id,
            command_id=command.command_id,
            code=code,
            severity="critical",
            at=_clock_read(self._context.clock),
            details={
                "source": "fresh_testnet_protection_inspection",
                "account_snapshot_hash": snapshot.snapshot_hash,
                "instrument": position.instrument,
                "durable_signed_quantity": canonical_decimal(
                    position.signed_quantity
                ),
                "venue_signed_quantity": canonical_decimal(venue_quantity),
                "venue_protected_quantity": canonical_decimal(covered),
            },
        )
        changed = _changed(before, _event_tip(self._context.store))
        return HandlerResult(
            HandlerDisposition.PROGRESSED if changed else HandlerDisposition.NO_WORK,
            changed,
        )

    def inspect_next(self) -> HandlerResult:
        return self._inspect_snapshot(self._context.snapshot())


class TestnetSafetyActionHandler:
    """Run one deterministic safety-controller evaluation on a fresh snapshot."""

    def __init__(self, context: _TestnetAccountContext) -> None:
        if type(context) is not _TestnetAccountContext:
            raise TypeError("context must be the exact TESTNET account context")
        self._context = context
        self._prepared: dict[str, tuple[RecoveryCommand, PreparedRecovery]] = {}
        self._prepared_lock = threading.Lock()

    @staticmethod
    def _recovery_binding(command: RecoveryCommand) -> tuple[object, ...]:
        return (
            command.recovery_command_id,
            command.permit_id,
            command.parent_command_id,
            command.incident_id,
            command.kind,
            command.priority,
            command.source_hash,
            command.preflight_hash,
            command.recovery_hash,
            command.recovery_material_hash,
            command.safety_policy_hash,
            command.original_attempt_id,
            command.original_nonce,
        )

    @staticmethod
    def _preparation_is_current(
        prepared: PreparedRecovery,
        *,
        at: datetime,
    ) -> bool:
        at_ms = _milliseconds(at)
        if at_ms >= prepared.action.expires_at_ms:
            return False
        evidence = prepared.evidence
        if type(evidence) is HyperliquidAccountSnapshot:
            age_ms = at_ms - evidence.server_time_ms
            return -_MAX_FUTURE_SKEW_MS <= age_ms <= _MAX_SNAPSHOT_AGE_MS
        return type(evidence) is AttemptRecord

    def _cache_prepared(
        self,
        result: SafetyControllerResult,
        *,
        at: datetime,
    ) -> bool:
        command = result.recovery_command
        prepared = result.prepared
        if command is not None and type(command) is not RecoveryCommand:
            raise TypeError("safety controller returned a non-durable recovery")
        if prepared is not None and type(prepared) is not PreparedRecovery:
            raise TypeError("safety controller returned an invalid preparation")
        with self._prepared_lock:
            if result.state in {SafetyControllerState.SAFE, SafetyControllerState.HALTED}:
                self._prepared.clear()
                return False
            if command is None:
                return False
            if prepared is not None:
                self._prepared = {
                    command.recovery_command_id: (command, prepared)
                }
            cached = self._prepared.get(command.recovery_command_id)
            if cached is None:
                return False
            if not self._preparation_is_current(cached[1], at=at):
                self._prepared.pop(command.recovery_command_id, None)
                return False
            return True

    def prepare(
        self,
        command: RecoveryCommand,
        *,
        at: datetime,
    ) -> PreparedRecovery:
        """Consume the exact one-shot preparation cached by ``act_next``.

        This is the read-only preparer contract expected by the recovery
        dispatcher.  It never reconstructs an action after the dispatcher has
        claimed it and never refetches an account snapshot.
        """

        if type(command) is not RecoveryCommand:
            raise TypeError("command must be an exact RecoveryCommand")
        checked_at = _utc(at, field="recovery preparation time")
        persisted = self._context.store.get_recovery_command(
            command.recovery_command_id
        )
        if type(persisted) is not RecoveryCommand or persisted != command:
            raise StateConflict("recovery preparation command is not exact durable state")
        with self._prepared_lock:
            cached = self._prepared.get(command.recovery_command_id)
            if cached is None:
                raise StateConflict("no exact safety preparation is cached")
            cached_command, prepared = cached
            if self._recovery_binding(cached_command) != self._recovery_binding(command):
                raise StateConflict(
                    "cached safety preparation differs from durable command"
                )
            if not self._preparation_is_current(prepared, at=checked_at):
                self._prepared.pop(command.recovery_command_id, None)
                raise StateConflict("cached safety preparation is stale or expired")
            self._prepared.pop(command.recovery_command_id)
        return prepared

    def _act_snapshot(
        self,
        snapshot: HyperliquidAccountSnapshot,
        *,
        startup: bool,
    ) -> HandlerResult:
        before = _event_tip(self._context.store)
        at = _clock_read(self._context.clock)
        result = self._context.safety_controller.evaluate(
            snapshot,
            self._context.market_brief(snapshot),
            at=at,
        )
        recovery_ready = self._cache_prepared(result, at=at)
        changed = _changed(before, _event_tip(self._context.store))
        return _handler_from_safety(
            result,
            local_state_changed=changed,
            startup=startup,
            recovery_ready=recovery_ready,
        )

    def act_next(self) -> HandlerResult:
        return self._act_snapshot(self._context.snapshot(), startup=False)


class TestnetStartupReconciliationHandler:
    """Prove one complete account view before the runtime may become ready."""

    def __init__(
        self,
        context: _TestnetAccountContext,
        protection_handler: TestnetProtectionInspectionHandler,
        safety_handler: TestnetSafetyActionHandler,
    ) -> None:
        if type(context) is not _TestnetAccountContext:
            raise TypeError("context must be the exact TESTNET account context")
        if type(protection_handler) is not TestnetProtectionInspectionHandler:
            raise TypeError("protection_handler has an invalid implementation")
        if type(safety_handler) is not TestnetSafetyActionHandler:
            raise TypeError("safety_handler has an invalid implementation")
        if (
            protection_handler._context is not context
            or safety_handler._context is not context
        ):
            raise StateConflict("startup handlers do not share one account context")
        self._context = context
        self._protection_handler = protection_handler
        self._safety_handler = safety_handler

    def reconcile_startup(self) -> HandlerResult:
        commands, _ = _command_view(self._context.store)
        recoveries, _ = _recovery_view(self._context.store)
        if any(
            item.state in _MAIN_RECONCILIATION_STATES
            for item in commands.values()
        ) or any(
            item.state in _RECOVERY_RECONCILIATION_STATES
            for item in recoveries.values()
        ):
            return HandlerResult(HandlerDisposition.WAITING, False)
        snapshot = self._context.snapshot()
        inspection = self._protection_handler._inspect_snapshot(snapshot)
        if inspection.disposition is HandlerDisposition.PROGRESSED:
            return inspection
        return self._safety_handler._act_snapshot(snapshot, startup=True)


@dataclass(frozen=True, slots=True)
class TestnetExecutorHandlerSet:
    """The five read/reconciliation ports consumed by ``ExecutorRuntime``."""

    startup_reconciler: TestnetStartupReconciliationHandler
    recovery_reconciler: TestnetRecoveryReconciliationHandler
    parent_reconciler: TestnetParentReconciliationHandler
    protection_inspector: TestnetProtectionInspectionHandler
    safety_handler: TestnetSafetyActionHandler

    def runtime_ports(self) -> dict[str, object]:
        return {
            "startup_reconciler": self.startup_reconciler,
            "recovery_reconciler": self.recovery_reconciler,
            "parent_reconciler": self.parent_reconciler,
            "protection_inspector": self.protection_inspector,
            "safety_handler": self.safety_handler,
        }


def build_testnet_executor_handlers(
    *,
    store: ExecutionStore,
    account_reader: AccountSnapshotReader,
    main_coordinator: MainEntryReconciliationCoordinator,
    venue_reconciler: HyperliquidVenueReconciler,
    recovery_coordinator: RecoveryReconciliationCoordinator,
    recovery_venue_reader: HyperliquidRecoveryVenueReader,
    safety_controller: TestnetAccountSafetyController,
    worker_id: str,
    market_brief_reader: MarketBriefReader | None = None,
    clock: Clock | None = None,
    reconciliation_lease_seconds: int = 15,
) -> TestnetExecutorHandlerSet:
    """Bind exact TESTNET components without introducing a write capability."""

    context = _TestnetAccountContext(
        store=store,
        safety_controller=safety_controller,
        account_reader=account_reader,
        market_brief_reader=market_brief_reader,
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )
    protection = TestnetProtectionInspectionHandler(context)
    safety = TestnetSafetyActionHandler(context)
    return TestnetExecutorHandlerSet(
        startup_reconciler=TestnetStartupReconciliationHandler(
            context,
            protection,
            safety,
        ),
        recovery_reconciler=TestnetRecoveryReconciliationHandler(
            context,
            recovery_coordinator,
            recovery_venue_reader,
            worker_id=worker_id,
        ),
        parent_reconciler=TestnetParentReconciliationHandler(
            context,
            main_coordinator,
            venue_reconciler,
            worker_id=worker_id,
            lease_seconds=reconciliation_lease_seconds,
        ),
        protection_inspector=protection,
        safety_handler=safety,
    )


__all__ = (
    "AccountSnapshotReader",
    "MarketBriefReader",
    "TestnetExecutorHandlerSet",
    "TestnetParentReconciliationHandler",
    "TestnetProtectionInspectionHandler",
    "TestnetRecoveryReconciliationHandler",
    "TestnetSafetyActionHandler",
    "TestnetStartupReconciliationHandler",
    "build_testnet_executor_handlers",
)
