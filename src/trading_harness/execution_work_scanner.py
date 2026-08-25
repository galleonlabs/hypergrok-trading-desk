"""Read-only, public-API-only discovery of durable executor work.

The scanner accepts the exact :class:`ExecutionStore` implementation so an
adapter or subclass cannot reinterpret capital state.  It never opens the
SQLite database and never calls claim/mutation methods.  Until ExecutionStore
offers every reviewed list method required by this contract, the scanner
returns a typed incompatibility result naming those methods and no work.

When the methods exist, their already-verified domain records are
cross-checked and projected into redacted work fingerprints.  Raw command,
incident, instrument, and recovery identifiers do not leave this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any

from .canonical import domain_hash
from .errors import StorageError, ValidationError
from .execution_store import (
    CommandRecord,
    ExecutionStore,
    IncidentRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryCommand,
    RecoveryOutbox,
)


Clock = Callable[[], datetime]
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
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
_PROTECTION_STATES = frozenset(
    {"flat", "protected", "under_protected", "over_protected", "failed"}
)


class ExecutionWorkKind(str, Enum):
    RECOVERY_DISPATCH = "recovery_dispatch"
    RECOVERY_RECONCILE = "recovery_reconcile"
    RECOVERY_IN_FLIGHT = "recovery_in_flight"
    PROTECTION_GAP = "protection_gap"
    OPEN_INCIDENT = "open_incident"
    COMMAND_RECONCILE = "command_reconcile"
    COMMAND_DISPATCH = "command_dispatch"
    COMMAND_IN_FLIGHT = "command_in_flight"
    OPEN_POSITION = "open_position"


@dataclass(frozen=True, slots=True)
class RequiredExecutionStoreMethod:
    name: str
    contract: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "contract": self.contract}


REQUIRED_EXECUTION_STORE_METHODS = (
    RequiredExecutionStoreMethod(
        "list_commands", "() -> tuple[CommandRecord, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_outboxes", "() -> tuple[OutboxRecord, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_recovery_commands", "() -> tuple[RecoveryCommand, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_recovery_outboxes", "() -> tuple[RecoveryOutbox, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_positions", "() -> tuple[PositionRecord, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_protections", "() -> tuple[ProtectionRecord, ...]"
    ),
    RequiredExecutionStoreMethod(
        "list_incidents", "() -> tuple[IncidentRecord, ...]"
    ),
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
        return _utc(clock(), field="clock")
    except ValidationError:
        raise
    except Exception as error:
        raise StorageError("execution work scanner clock failed") from error


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fingerprint(kind: ExecutionWorkKind, value: str) -> str:
    return domain_hash(f"trading-harness/execution-work/{kind.value}/v1", value)


@dataclass(frozen=True, slots=True)
class ExecutionWorkItem:
    kind: ExecutionWorkKind
    reference_fingerprint: str
    state: str
    priority: int
    updated_at: datetime
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExecutionWorkKind):
            raise TypeError("kind must be ExecutionWorkKind")
        if not isinstance(self.reference_fingerprint, str) or not _HASH_RE.fullmatch(
            self.reference_fingerprint
        ):
            raise ValidationError("reference_fingerprint is invalid")
        if not isinstance(self.state, str) or not self.state:
            raise ValidationError("work item state is invalid")
        if type(self.priority) is not int or not 0 <= self.priority <= 100:
            raise ValidationError("work item priority is invalid")
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field="updated_at"))
        if not isinstance(self.detail, str) or not self.detail:
            raise ValidationError("work item detail is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "reference_fingerprint": self.reference_fingerprint,
            "state": self.state,
            "priority": self.priority,
            "updated_at": _time_text(self.updated_at),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExecutionWorkScan:
    compatible: bool
    missing_methods: tuple[RequiredExecutionStoreMethod, ...]
    observed_at: datetime
    items: tuple[ExecutionWorkItem, ...]
    command_count: int
    recovery_count: int
    position_count: int
    protection_gap_count: int
    open_incident_count: int
    scan_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "compatible": self.compatible,
            "missing_methods": [item.as_dict() for item in self.missing_methods],
            "observed_at": _time_text(self.observed_at),
            "items": [item.as_dict() for item in self.items],
            "command_count": self.command_count,
            "recovery_count": self.recovery_count,
            "position_count": self.position_count,
            "protection_gap_count": self.protection_gap_count,
            "open_incident_count": self.open_incident_count,
            "scan_hash": self.scan_hash,
        }


def _scan_result(
    *,
    compatible: bool,
    missing: tuple[RequiredExecutionStoreMethod, ...],
    observed_at: datetime,
    items: tuple[ExecutionWorkItem, ...],
    command_count: int,
    recovery_count: int,
    position_count: int,
    protection_gap_count: int,
    open_incident_count: int,
) -> ExecutionWorkScan:
    material = {
        "compatible": compatible,
        "missing_methods": tuple(item.as_dict() for item in missing),
        "observed_at": observed_at,
        "items": tuple(item.as_dict() for item in items),
        "command_count": command_count,
        "recovery_count": recovery_count,
        "position_count": position_count,
        "protection_gap_count": protection_gap_count,
        "open_incident_count": open_incident_count,
    }
    return ExecutionWorkScan(
        compatible=compatible,
        missing_methods=missing,
        observed_at=observed_at,
        items=items,
        command_count=command_count,
        recovery_count=recovery_count,
        position_count=position_count,
        protection_gap_count=protection_gap_count,
        open_incident_count=open_incident_count,
        scan_hash=domain_hash("trading-harness/execution-work-scan/v1", material),
    )


class ExecutionWorkScanner:
    """Inspect verified public record lists without touching store internals."""

    def __init__(self, store: ExecutionStore, *, clock: Clock | None = None) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be the exact ExecutionStore implementation")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _available_method(name: str) -> object | None:
        candidate = getattr(ExecutionStore, name, None)
        return candidate if callable(candidate) else None

    def compatibility(self) -> tuple[RequiredExecutionStoreMethod, ...]:
        return tuple(
            requirement
            for requirement in REQUIRED_EXECUTION_STORE_METHODS
            if self._available_method(requirement.name) is None
        )

    def _call_list(self, name: str, expected_type: type[Any]) -> tuple[Any, ...]:
        descriptor = self._available_method(name)
        if descriptor is None:  # pragma: no cover - guarded by compatibility
            raise StorageError("execution store list contract disappeared during scan")
        try:
            result = descriptor.__get__(self._store, ExecutionStore)()
        except (StorageError, ValidationError):
            raise
        except Exception as error:
            raise StorageError("execution store public list method failed") from error
        if not isinstance(result, tuple) or any(type(item) is not expected_type for item in result):
            raise StorageError(f"execution store {name} returned an invalid record collection")
        return result

    @staticmethod
    def _unique(records: tuple[Any, ...], field: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for record in records:
            value = getattr(record, field, None)
            if not isinstance(value, str) or not value or value in result:
                raise StorageError(f"execution store returned duplicate or invalid {field}")
            result[value] = record
        return result

    def scan(self) -> ExecutionWorkScan:
        observed = _clock_read(self._clock)
        missing = self.compatibility()
        if missing:
            # Do not consume even the currently available list APIs.  A partial
            # view could incorrectly describe the account as safe or idle.
            return _scan_result(
                compatible=False,
                missing=missing,
                observed_at=observed,
                items=(),
                command_count=0,
                recovery_count=0,
                position_count=0,
                protection_gap_count=0,
                open_incident_count=0,
            )

        commands = self._call_list("list_commands", CommandRecord)
        outboxes = self._call_list("list_outboxes", OutboxRecord)
        recoveries = self._call_list("list_recovery_commands", RecoveryCommand)
        recovery_outboxes = self._call_list(
            "list_recovery_outboxes", RecoveryOutbox
        )
        positions = self._call_list("list_positions", PositionRecord)
        protections = self._call_list("list_protections", ProtectionRecord)
        incidents = self._call_list("list_incidents", IncidentRecord)

        command_map = self._unique(commands, "command_id")
        outbox_map = self._unique(outboxes, "command_id")
        recovery_map = self._unique(recoveries, "recovery_command_id")
        recovery_outbox_map = self._unique(
            recovery_outboxes, "recovery_command_id"
        )
        self._unique(positions, "instrument")
        self._unique(protections, "command_id")
        self._unique(incidents, "incident_id")
        if set(command_map) != set(outbox_map):
            raise StorageError("execution command and outbox public views disagree")
        if set(recovery_map) != set(recovery_outbox_map):
            raise StorageError("recovery command and outbox public views disagree")

        items: list[ExecutionWorkItem] = []
        for command_id, command in command_map.items():
            outbox = outbox_map[command_id]
            if command.state not in _COMMAND_STATES or outbox.state != command.state:
                raise StorageError("execution command public state is invalid")
            if command.state == "terminal":
                continue
            if command.state == "queued":
                kind, priority, detail = (
                    ExecutionWorkKind.COMMAND_DISPATCH,
                    40,
                    "verified command awaits dispatch claim",
                )
            elif command.state == "claimed":
                if outbox.lease_expires_at is None:
                    raise StorageError("claimed command lacks a public lease expiry")
                if outbox.lease_expires_at <= observed:
                    if outbox.current_attempt_id is None:
                        kind, priority, detail = (
                            ExecutionWorkKind.COMMAND_DISPATCH,
                            35,
                            "verified unsent dispatch claim has expired",
                        )
                    else:
                        kind, priority, detail = (
                            ExecutionWorkKind.COMMAND_RECONCILE,
                            10,
                            "expired dispatch claim has a durable attempt",
                        )
                else:
                    kind, priority, detail = (
                        ExecutionWorkKind.COMMAND_IN_FLIGHT,
                        30,
                        "verified command has an active dispatch claim",
                    )
            else:
                kind, priority, detail = (
                    ExecutionWorkKind.COMMAND_RECONCILE,
                    20,
                    "verified command requires venue reconciliation",
                )
            items.append(
                ExecutionWorkItem(
                    kind=kind,
                    reference_fingerprint=_fingerprint(kind, command_id),
                    state=command.state,
                    priority=priority,
                    updated_at=max(command.updated_at, outbox.updated_at),
                    detail=detail,
                )
            )

        for recovery_id, recovery in recovery_map.items():
            outbox = recovery_outbox_map[recovery_id]
            if recovery.state not in _RECOVERY_STATES or outbox.state != recovery.state:
                raise StorageError("recovery command public state is invalid")
            if recovery.state == "terminal":
                continue
            if recovery.state == "queued":
                kind, detail = (
                    ExecutionWorkKind.RECOVERY_DISPATCH,
                    "verified account-safety recovery awaits dispatch",
                )
            elif recovery.state in {"claimed", "signing"}:
                if outbox.lease_expires_at is None:
                    raise StorageError("claimed recovery lacks a public lease expiry")
                if outbox.lease_expires_at <= observed:
                    if outbox.current_attempt_id is None:
                        kind, detail = (
                            ExecutionWorkKind.RECOVERY_DISPATCH,
                            "verified unsent recovery claim has expired",
                        )
                    else:
                        kind, detail = (
                            ExecutionWorkKind.RECOVERY_RECONCILE,
                            "expired recovery claim has a durable attempt",
                        )
                else:
                    kind, detail = (
                        ExecutionWorkKind.RECOVERY_IN_FLIGHT,
                        "verified account-safety recovery is in flight",
                    )
            else:
                kind, detail = (
                    ExecutionWorkKind.RECOVERY_RECONCILE,
                    "verified account-safety recovery requires reconciliation",
                )
            priority = recovery.priority
            if type(priority) is not int or not 0 <= priority <= 2:
                raise StorageError("recovery priority is invalid")
            items.append(
                ExecutionWorkItem(
                    kind=kind,
                    reference_fingerprint=_fingerprint(kind, recovery_id),
                    state=recovery.state,
                    priority=priority,
                    updated_at=max(recovery.updated_at, outbox.updated_at),
                    detail=detail,
                )
            )

        nonzero_positions = 0
        for position in positions:
            if not isinstance(position.signed_quantity, Decimal):
                raise StorageError("position quantity is not exact Decimal")
            if position.signed_quantity == 0:
                continue
            nonzero_positions += 1
            kind = ExecutionWorkKind.OPEN_POSITION
            items.append(
                ExecutionWorkItem(
                    kind=kind,
                    reference_fingerprint=_fingerprint(kind, position.instrument),
                    state="open",
                    priority=15,
                    updated_at=position.observed_at,
                    detail="verified account position remains open",
                )
            )

        protection_gaps = 0
        for protection in protections:
            if protection.state not in _PROTECTION_STATES:
                raise StorageError("protection public state is invalid")
            if protection.state in {"flat", "protected"}:
                continue
            protection_gaps += 1
            kind = ExecutionWorkKind.PROTECTION_GAP
            items.append(
                ExecutionWorkItem(
                    kind=kind,
                    reference_fingerprint=_fingerprint(kind, protection.command_id),
                    state=protection.state,
                    priority=0,
                    updated_at=protection.observed_at,
                    detail="verified position protection is not exact",
                )
            )

        open_incidents = 0
        for incident in incidents:
            if incident.state not in {"open", "contained", "closed"}:
                raise StorageError("incident public state is invalid")
            if incident.state != "open":
                continue
            open_incidents += 1
            if incident.severity not in {"warning", "high", "critical"}:
                raise StorageError("incident severity is invalid")
            priority = {"critical": 0, "high": 5, "warning": 10}[incident.severity]
            kind = ExecutionWorkKind.OPEN_INCIDENT
            items.append(
                ExecutionWorkItem(
                    kind=kind,
                    reference_fingerprint=_fingerprint(kind, incident.incident_id),
                    state=incident.severity,
                    priority=priority,
                    updated_at=incident.updated_at,
                    detail="verified execution incident remains open",
                )
            )

        ordered = tuple(
            sorted(
                items,
                key=lambda item: (
                    item.priority,
                    item.kind.value,
                    item.reference_fingerprint,
                ),
            )
        )
        return _scan_result(
            compatible=True,
            missing=(),
            observed_at=observed,
            items=ordered,
            command_count=sum(command.state != "terminal" for command in commands),
            recovery_count=sum(
                recovery.state != "terminal" for recovery in recoveries
            ),
            position_count=nonzero_positions,
            protection_gap_count=protection_gaps,
            open_incident_count=open_incidents,
        )


__all__ = (
    "REQUIRED_EXECUTION_STORE_METHODS",
    "ExecutionWorkItem",
    "ExecutionWorkKind",
    "ExecutionWorkScan",
    "ExecutionWorkScanner",
    "RequiredExecutionStoreMethod",
)
