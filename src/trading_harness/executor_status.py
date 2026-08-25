"""Redacted status contracts for the isolated executor.

Status is intentionally a lossy view.  Account identifiers, public addresses,
Keychain labels, filesystem paths, source references, and exception messages
are represented only by domain-separated fingerprints or fixed enums.  The
model therefore remains safe to expose through a local status CLI later
without turning failures into a metadata or secret disclosure channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import re

from .canonical import domain_hash
from .daily_loss import DailyLossSnapshot
from .domain import Environment
from .errors import ValidationError
from .executor_config import ExecutorConfig


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutorProcessState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


class ExecutorRiskGate(str, Enum):
    HALTED = "halted"
    RECONCILING = "reconciling"
    READY = "ready"


class ExecutorBlocker(str, Enum):
    PROCESS_NOT_RUNNING = "process_not_running"
    CONFIG_DRIFT = "config_drift"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    ACCOUNT_RECONCILIATION_INCOMPLETE = "account_reconciliation_incomplete"
    DAILY_LOSS_COVERAGE_INCOMPLETE = "daily_loss_coverage_incomplete"
    DAILY_LOSS_SNAPSHOT_STALE = "daily_loss_snapshot_stale"
    DAILY_LOSS_LIMIT_REACHED = "daily_loss_limit_reached"
    HEARTBEAT_UNAVAILABLE = "heartbeat_unavailable"
    HEARTBEAT_STALE = "heartbeat_stale"
    MANUAL_HALT = "manual_halt"


def _utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fingerprint(domain: str, value: object) -> str:
    return domain_hash(domain, value)


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class RedactedExecutorConfigStatus:
    schema_version: int
    environment: Environment
    venue: str
    config_hash: str
    node_fingerprint: str
    account_fingerprint: str
    main_account_fingerprint: str
    api_wallet_fingerprint: str
    credential_service_fingerprint: str
    credential_account_fingerprint: str
    approval_service_fingerprint: str
    approval_account_fingerprint: str
    recovery_service_fingerprint: str
    recovery_account_fingerprint: str
    grant_service_fingerprint: str
    grant_account_fingerprint: str
    recovery_cloids_fingerprint: str
    execution_database_fingerprint: str
    nonce_database_fingerprint: str
    daily_loss_database_fingerprint: str
    learning_database_fingerprint: str
    staging_database_fingerprint: str
    control_socket_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValidationError("schema_version must be positive")
        if self.environment is not Environment.TESTNET:
            raise ValidationError("redacted executor status supports TESTNET only")
        if self.venue != "hyperliquid":
            raise ValidationError("status venue must be hyperliquid")
        for field in (
            "config_hash",
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
        ):
            _sha256(getattr(self, field), field=field)

    @classmethod
    def from_config(cls, config: ExecutorConfig) -> "RedactedExecutorConfigStatus":
        if not isinstance(config, ExecutorConfig):
            raise TypeError("config must be ExecutorConfig")
        return cls(
            schema_version=config.schema_version,
            environment=config.environment,
            venue=config.venue,
            config_hash=config.config_hash,
            node_fingerprint=_fingerprint(
                "trading-harness/status-node/v1", config.node_id
            ),
            account_fingerprint=_fingerprint(
                "trading-harness/status-account/v1", config.account_id
            ),
            main_account_fingerprint=_fingerprint(
                "trading-harness/status-main-account/v1",
                config.main_account_address,
            ),
            api_wallet_fingerprint=_fingerprint(
                "trading-harness/status-api-wallet/v1", config.api_wallet_address
            ),
            credential_service_fingerprint=_fingerprint(
                "trading-harness/status-keychain-service/v1",
                config.credential.service,
            ),
            credential_account_fingerprint=_fingerprint(
                "trading-harness/status-keychain-account/v1",
                config.credential.account,
            ),
            approval_service_fingerprint=_fingerprint(
                "trading-harness/status-approval-keychain-service/v1",
                config.approval_credential.service,
            ),
            approval_account_fingerprint=_fingerprint(
                "trading-harness/status-approval-keychain-account/v1",
                config.approval_credential.account,
            ),
            recovery_service_fingerprint=_fingerprint(
                "trading-harness/status-recovery-keychain-service/v1",
                config.recovery_credential.service,
            ),
            recovery_account_fingerprint=_fingerprint(
                "trading-harness/status-recovery-keychain-account/v1",
                config.recovery_credential.account,
            ),
            grant_service_fingerprint=_fingerprint(
                "trading-harness/status-grant-keychain-service/v1",
                config.grant_credential.service,
            ),
            grant_account_fingerprint=_fingerprint(
                "trading-harness/status-grant-keychain-account/v1",
                config.grant_credential.account,
            ),
            recovery_cloids_fingerprint=_fingerprint(
                "trading-harness/status-recovery-cloids/v1",
                tuple(sorted(config.recovery_cloids)),
            ),
            execution_database_fingerprint=_fingerprint(
                "trading-harness/status-execution-database/v1",
                str(config.paths.execution_database),
            ),
            nonce_database_fingerprint=_fingerprint(
                "trading-harness/status-nonce-database/v1",
                str(config.paths.nonce_database),
            ),
            daily_loss_database_fingerprint=_fingerprint(
                "trading-harness/status-daily-loss-database/v1",
                str(config.paths.daily_loss_database),
            ),
            learning_database_fingerprint=_fingerprint(
                "trading-harness/status-learning-database/v1",
                str(config.paths.learning_database),
            ),
            staging_database_fingerprint=_fingerprint(
                "trading-harness/status-staging-database/v1",
                str(config.paths.staging_database),
            ),
            control_socket_fingerprint=_fingerprint(
                "trading-harness/status-control-socket/v1",
                str(config.paths.control_socket),
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment.value,
            "venue": self.venue,
            "config_hash": self.config_hash,
            "node_fingerprint": self.node_fingerprint,
            "account_fingerprint": self.account_fingerprint,
            "main_account_fingerprint": self.main_account_fingerprint,
            "api_wallet_fingerprint": self.api_wallet_fingerprint,
            "credential_service_fingerprint": self.credential_service_fingerprint,
            "credential_account_fingerprint": self.credential_account_fingerprint,
            "approval_service_fingerprint": self.approval_service_fingerprint,
            "approval_account_fingerprint": self.approval_account_fingerprint,
            "recovery_service_fingerprint": self.recovery_service_fingerprint,
            "recovery_account_fingerprint": self.recovery_account_fingerprint,
            "grant_service_fingerprint": self.grant_service_fingerprint,
            "grant_account_fingerprint": self.grant_account_fingerprint,
            "recovery_cloids_fingerprint": self.recovery_cloids_fingerprint,
            "execution_database_fingerprint": self.execution_database_fingerprint,
            "nonce_database_fingerprint": self.nonce_database_fingerprint,
            "daily_loss_database_fingerprint": self.daily_loss_database_fingerprint,
            "learning_database_fingerprint": self.learning_database_fingerprint,
            "staging_database_fingerprint": self.staging_database_fingerprint,
            "control_socket_fingerprint": self.control_socket_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ExecutorStatus:
    config: RedactedExecutorConfigStatus
    process_state: ExecutorProcessState
    declared_risk_gate: ExecutorRiskGate
    effective_risk_gate: ExecutorRiskGate
    started_at: datetime
    observed_at: datetime
    heartbeat_at: datetime | None
    credential_loaded: bool
    account_reconciled: bool
    config_matches_durable_state: bool
    blockers: tuple[ExecutorBlocker, ...]
    daily_loss: DailyLossSnapshot | None
    status_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.config, RedactedExecutorConfigStatus):
            raise TypeError("config must be RedactedExecutorConfigStatus")
        if not isinstance(self.process_state, ExecutorProcessState):
            raise TypeError("process_state must be ExecutorProcessState")
        if not isinstance(self.declared_risk_gate, ExecutorRiskGate):
            raise TypeError("declared_risk_gate must be ExecutorRiskGate")
        if not isinstance(self.effective_risk_gate, ExecutorRiskGate):
            raise TypeError("effective_risk_gate must be ExecutorRiskGate")
        started = _utc(self.started_at, field="started_at")
        observed = _utc(self.observed_at, field="observed_at")
        heartbeat = (
            None
            if self.heartbeat_at is None
            else _utc(self.heartbeat_at, field="heartbeat_at")
        )
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "heartbeat_at", heartbeat)
        if started > observed or (
            heartbeat is not None and (heartbeat < started or heartbeat > observed)
        ):
            raise ValidationError("executor status timestamps are inconsistent")
        for field in (
            "credential_loaded",
            "account_reconciled",
            "config_matches_durable_state",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")
        if not isinstance(self.blockers, tuple) or any(
            not isinstance(item, ExecutorBlocker) for item in self.blockers
        ):
            raise TypeError("blockers must be a tuple of ExecutorBlocker")
        if self.blockers != tuple(sorted(set(self.blockers), key=lambda item: item.value)):
            raise ValidationError("blockers must be unique and canonically ordered")
        if self.daily_loss is not None and not isinstance(
            self.daily_loss, DailyLossSnapshot
        ):
            raise TypeError("daily_loss must be DailyLossSnapshot or None")
        if self.effective_risk_gate is ExecutorRiskGate.READY and (
            self.declared_risk_gate is not ExecutorRiskGate.READY
            or self.process_state is not ExecutorProcessState.RUNNING
            or self.blockers
        ):
            raise ValidationError("effective ready gate contradicts executor status")
        checked_hash = _sha256(self.status_hash, field="status_hash")
        expected_hash = domain_hash(
            "trading-harness/executor-status/v1",
            {
                "config": self.config.as_dict(),
                "process_state": self.process_state.value,
                "declared_risk_gate": self.declared_risk_gate.value,
                "effective_risk_gate": self.effective_risk_gate.value,
                "started_at": started,
                "observed_at": observed,
                "heartbeat_at": heartbeat,
                "credential_loaded": self.credential_loaded,
                "account_reconciled": self.account_reconciled,
                "config_matches_durable_state": self.config_matches_durable_state,
                "blockers": tuple(item.value for item in self.blockers),
                "daily_loss_snapshot_hash": (
                    None if self.daily_loss is None else self.daily_loss.snapshot_hash
                ),
            },
        )
        if checked_hash != expected_hash:
            raise ValidationError("executor status hash does not match")

    def as_dict(self) -> dict[str, object]:
        return {
            "config": self.config.as_dict(),
            "process_state": self.process_state.value,
            "declared_risk_gate": self.declared_risk_gate.value,
            "effective_risk_gate": self.effective_risk_gate.value,
            "started_at": _time_text(self.started_at),
            "observed_at": _time_text(self.observed_at),
            "heartbeat_at": (
                None if self.heartbeat_at is None else _time_text(self.heartbeat_at)
            ),
            "credential_loaded": self.credential_loaded,
            "account_reconciled": self.account_reconciled,
            "config_matches_durable_state": self.config_matches_durable_state,
            "blockers": [blocker.value for blocker in self.blockers],
            "daily_loss": (
                None if self.daily_loss is None else self.daily_loss.as_dict()
            ),
            "status_hash": self.status_hash,
        }


def build_executor_status(
    *,
    config: ExecutorConfig,
    process_state: ExecutorProcessState | str,
    declared_risk_gate: ExecutorRiskGate | str,
    started_at: datetime,
    observed_at: datetime,
    heartbeat_at: datetime | None,
    credential_loaded: bool,
    account_reconciled: bool,
    config_matches_durable_state: bool,
    daily_loss: DailyLossSnapshot | None,
    manual_halt: bool = False,
) -> ExecutorStatus:
    """Build a redacted status and independently derive its effective gate."""

    if not isinstance(config, ExecutorConfig):
        raise TypeError("config must be ExecutorConfig")
    try:
        selected_process = (
            process_state
            if isinstance(process_state, ExecutorProcessState)
            else ExecutorProcessState(process_state)
        )
        selected_gate = (
            declared_risk_gate
            if isinstance(declared_risk_gate, ExecutorRiskGate)
            else ExecutorRiskGate(declared_risk_gate)
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("executor status state is invalid") from error
    for field, value in (
        ("credential_loaded", credential_loaded),
        ("account_reconciled", account_reconciled),
        ("config_matches_durable_state", config_matches_durable_state),
        ("manual_halt", manual_halt),
    ):
        if type(value) is not bool:
            raise TypeError(f"{field} must be bool")
    started = _utc(started_at, field="started_at")
    observed = _utc(observed_at, field="observed_at")
    if started > observed:
        raise ValidationError("started_at may not follow observed_at")
    heartbeat = None if heartbeat_at is None else _utc(heartbeat_at, field="heartbeat_at")
    if heartbeat is not None and (heartbeat < started or heartbeat > observed):
        raise ValidationError("heartbeat_at is outside the runtime interval")
    if daily_loss is not None:
        if not isinstance(daily_loss, DailyLossSnapshot):
            raise TypeError("daily_loss must be DailyLossSnapshot or None")
        if daily_loss.as_of > observed:
            raise ValidationError("daily-loss snapshot follows status observation")
        if daily_loss.binding_hash != domain_hash(
            "trading-harness/daily-loss-binding/v1",
            {
                "account_id": config.account_id,
                "environment": config.environment,
                "config_hash": config.config_hash,
                "daily_loss_limit": config.daily_loss_limit,
                "settlement_currency": config.settlement_currency,
            },
        ):
            raise ValidationError("daily-loss snapshot binding does not match config")

    blockers: set[ExecutorBlocker] = set()
    if selected_process is not ExecutorProcessState.RUNNING:
        blockers.add(ExecutorBlocker.PROCESS_NOT_RUNNING)
    if not config_matches_durable_state:
        blockers.add(ExecutorBlocker.CONFIG_DRIFT)
    if not credential_loaded:
        blockers.add(ExecutorBlocker.CREDENTIAL_UNAVAILABLE)
    if not account_reconciled:
        blockers.add(ExecutorBlocker.ACCOUNT_RECONCILIATION_INCOMPLETE)
    if daily_loss is None or not daily_loss.coverage_complete:
        blockers.add(ExecutorBlocker.DAILY_LOSS_COVERAGE_INCOMPLETE)
    if daily_loss is not None and (
        observed - daily_loss.as_of
        > timedelta(milliseconds=config.reconcile_interval_ms * 2)
    ):
        blockers.add(ExecutorBlocker.DAILY_LOSS_SNAPSHOT_STALE)
    if daily_loss is not None and daily_loss.remaining <= 0:
        blockers.add(ExecutorBlocker.DAILY_LOSS_LIMIT_REACHED)
    if heartbeat is None:
        blockers.add(ExecutorBlocker.HEARTBEAT_UNAVAILABLE)
    elif (
        observed - heartbeat
        > timedelta(
            milliseconds=max(
                config.poll_interval_ms * 3,
                config.reconcile_interval_ms * 2,
            )
        )
    ):
        blockers.add(ExecutorBlocker.HEARTBEAT_STALE)
    if manual_halt:
        blockers.add(ExecutorBlocker.MANUAL_HALT)
    ordered = tuple(sorted(blockers, key=lambda item: item.value))
    effective_gate = selected_gate if not ordered else ExecutorRiskGate.HALTED
    redacted = RedactedExecutorConfigStatus.from_config(config)
    material = {
        "config": redacted.as_dict(),
        "process_state": selected_process.value,
        "declared_risk_gate": selected_gate.value,
        "effective_risk_gate": effective_gate.value,
        "started_at": started,
        "observed_at": observed,
        "heartbeat_at": heartbeat,
        "credential_loaded": credential_loaded,
        "account_reconciled": account_reconciled,
        "config_matches_durable_state": config_matches_durable_state,
        "blockers": tuple(item.value for item in ordered),
        "daily_loss_snapshot_hash": (
            None if daily_loss is None else daily_loss.snapshot_hash
        ),
    }
    return ExecutorStatus(
        config=redacted,
        process_state=selected_process,
        declared_risk_gate=selected_gate,
        effective_risk_gate=effective_gate,
        started_at=started,
        observed_at=observed,
        heartbeat_at=heartbeat,
        credential_loaded=credential_loaded,
        account_reconciled=account_reconciled,
        config_matches_durable_state=config_matches_durable_state,
        blockers=ordered,
        daily_loss=daily_loss,
        status_hash=domain_hash("trading-harness/executor-status/v1", material),
    )


__all__ = (
    "ExecutorBlocker",
    "ExecutorProcessState",
    "ExecutorRiskGate",
    "ExecutorStatus",
    "RedactedExecutorConfigStatus",
    "build_executor_status",
)
