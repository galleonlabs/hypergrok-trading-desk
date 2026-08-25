"""Deterministic TESTNET account-safety recovery preparation.

This module is the narrow bridge between verified read-only account truth and
the durable recovery outbox.  It never signs, submits, or accepts a caller-
chosen exchange action.  One invocation can only:

* return an already-active recovery;
* fence an unknown parent attempt with its exact original nonce;
* flatten the full fresh residual position through a bounded reduce-only IOC;
* cancel durable, owned orders after the account is globally flat; or
* halt without creating authority.

The exact source object used to construct a newly queued action is returned in
``PreparedRecovery``.  A dispatcher can therefore sign against that same
snapshot/attempt instead of refetching and producing a different source hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import (
    Context,
    Decimal,
    DecimalException,
    ROUND_CEILING,
    ROUND_FLOOR,
    localcontext,
)
from enum import Enum
import fcntl
import json
from pathlib import Path
from typing import Any, Iterator

from .approval import (
    TestnetRecoveryAuthority,
    verified_recovery_permit,
)
from .canonical import canonical_decimal
from .domain import Environment
from .errors import (
    AdmissionDenied,
    RecordNotFound,
    StateConflict,
    StorageError,
    ValidationError,
)
from .execution_store import (
    AttemptRecord,
    CommandRecord,
    ExecutionStore,
    IncidentRecord,
    LegRecord,
    OutboxRecord,
    PositionRecord,
    ProtectionRecord,
    RecoveryCommand,
    RecoveryOutbox,
)
from .hyperliquid_account import (
    HyperliquidAccountSnapshot,
    PositionSide,
)
from .hyperliquid_recovery import (
    CancelByCloidAction,
    CancelRequest,
    NoopFenceAction,
    RecoveryAction,
    RecoveryKind,
    ReduceOnlyCloseAction,
    ambiguous_attempt_hash,
    build_cancel_by_cloid,
    build_noop_fence,
    build_reduce_only_close,
    derive_recovery_close_cloid,
    recovery_action_from_material,
)
from .hyperliquid_signer import SignerPolicy, SigningAccount
from .hyperliquid_wire import HyperliquidNetwork, format_perp_price
from .market_data import public_info_endpoint
from .reconciliation_coordinator import _verify_snapshot_hash
from .recovery_dispatcher import PreparedRecovery


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO = Decimal("0")
_BASIS_POINTS = Decimal("10000")
_ARITHMETIC = Context(prec=256)
_RECOVERABLE_INCIDENT_CODES = frozenset(
    {
        "PROTECTION_SUBMISSION_FAILED",
        "PROTECTION_FAILED",
        "POSITION_UNDER_PROTECTED",
        "POSITION_OVER_PROTECTED",
        "POSITION_DIRECTION_CONTRADICTION",
        "RECOVERY_REQUIRED",
        "ACCOUNT_SAFETY_RECOVERY",
    }
)
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


class SafetyControllerState(str, Enum):
    SAFE = "safe"
    QUEUED = "queued"
    ACTIVE = "active"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class SafetyControllerPolicy:
    """Hard bounds for one deterministic safety-controller generation."""

    max_flatten_slippage_bps: Decimal = Decimal("25")
    max_account_snapshot_age_ms: int = 5_000
    max_market_age_ms: int = 5_000
    future_skew_ms: int = 1_000
    action_ttl_ms: int = 10_000
    permit_ttl_seconds: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.max_flatten_slippage_bps, Decimal):
            raise TypeError("max_flatten_slippage_bps must be Decimal")
        if (
            not self.max_flatten_slippage_bps.is_finite()
            or not _ZERO < self.max_flatten_slippage_bps <= Decimal("25")
        ):
            raise ValidationError(
                "max_flatten_slippage_bps must be positive and at most 25"
            )
        object.__setattr__(
            self,
            "max_flatten_slippage_bps",
            Decimal(canonical_decimal(self.max_flatten_slippage_bps)),
        )
        for field, lower, upper in (
            ("max_account_snapshot_age_ms", 1, 5_000),
            ("max_market_age_ms", 1, 5_000),
            ("future_skew_ms", 0, 5_000),
            ("action_ttl_ms", 1_000, 15_000),
            ("permit_ttl_seconds", 1, 15),
        ):
            value = getattr(self, field)
            if type(value) is not int or not lower <= value <= upper:
                raise ValidationError(f"{field} is outside its compiled bound")
        if self.permit_ttl_seconds * 1_000 != self.action_ttl_ms:
            raise ValidationError(
                "permit and action TTLs must be identical for deterministic expiry"
            )


@dataclass(frozen=True, slots=True)
class SafetyControllerResult:
    state: SafetyControllerState
    reason_code: str
    observed_at: datetime
    safety_policy_hash: str
    recovery_command: RecoveryCommand | None = None
    prepared: PreparedRecovery | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SafetyControllerState):
            raise TypeError("state must be SafetyControllerState")
        if (
            not isinstance(self.reason_code, str)
            or not self.reason_code
            or self.reason_code != self.reason_code.strip()
        ):
            raise ValidationError("reason_code is invalid")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        _hash(self.safety_policy_hash, "safety_policy_hash")
        if self.recovery_command is not None and not isinstance(
            self.recovery_command, RecoveryCommand
        ):
            raise TypeError("recovery_command must be RecoveryCommand or None")
        if self.prepared is not None and not isinstance(self.prepared, PreparedRecovery):
            raise TypeError("prepared must be PreparedRecovery or None")
        if self.state is SafetyControllerState.QUEUED and (
            self.recovery_command is None or self.prepared is None
        ):
            raise ValidationError("queued result requires command and exact preparation")
        if self.state in {SafetyControllerState.SAFE, SafetyControllerState.HALTED} and (
            self.recovery_command is not None or self.prepared is not None
        ):
            raise ValidationError("safe/halted result cannot carry recovery authority")


@dataclass(frozen=True, slots=True)
class _CommandView:
    command: CommandRecord
    outbox: OutboxRecord
    plan: Mapping[str, Any]
    legs: tuple[LegRecord, ...]
    symbol: str
    entry_side: str
    stop_cloid: str
    cloids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _StoreView:
    commands: Mapping[str, _CommandView]
    incidents: Mapping[str, IncidentRecord]
    recoveries: Mapping[str, RecoveryCommand]
    recovery_outboxes: Mapping[str, RecoveryOutbox]
    positions: tuple[PositionRecord, ...]
    protections: tuple[ProtectionRecord, ...]


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{field} is outside the supported UTC range") from error


def _milliseconds(value: datetime) -> int:
    delta = value - _EPOCH
    result = delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000
    if result < 0:
        raise ValidationError("time predates the Unix epoch")
    return result


def _hash(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _decimal_string(value: object, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a canonical decimal string")
    try:
        result = Decimal(value)
    except (ArithmeticError, ValueError) as error:
        raise ValidationError(f"{field} is not a decimal") from error
    if (
        not result.is_finite()
        or canonical_decimal(result) != value
        or (positive and result <= _ZERO)
    ):
        raise ValidationError(f"{field} is not a bounded canonical decimal")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValidationError(f"{field} must be an object")
    return value


def _verified_policy(policy: SignerPolicy) -> SignerPolicy:
    if not isinstance(policy, SignerPolicy):
        raise TypeError("signer_policy must be SignerPolicy")
    try:
        verified = SignerPolicy(
            accounts=policy.accounts,
            allowed_asset_ids=policy.allowed_asset_ids,
            allowed_networks=policy.allowed_networks,
            allow_mainnet=policy.allow_mainnet,
            minimum_expiry_remaining_ms=policy.minimum_expiry_remaining_ms,
            maximum_expiry_horizon_ms=policy.maximum_expiry_horizon_ms,
            allowed_recovery_kinds=policy.allowed_recovery_kinds,
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ValidationError("signer policy failed exact reconstruction") from error
    if verified != policy:
        raise ValidationError("signer policy differs from its verified reconstruction")
    return verified


def _unique(records: tuple[Any, ...], field: str, expected: type[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        if type(record) is not expected:
            raise StorageError(f"execution store returned a non-{expected.__name__} record")
        identity = getattr(record, field, None)
        if not isinstance(identity, str) or not identity or identity in result:
            raise StorageError(f"execution store returned duplicate or invalid {field}")
        result[identity] = record
    return result


def _symbol_from_instrument(
    snapshot: HyperliquidAccountSnapshot,
    instrument: object,
) -> str:
    if not isinstance(instrument, str) or not instrument:
        raise StorageError("durable plan lacks an instrument")
    matches = tuple(
        item.symbol
        for item in snapshot.metadata.instruments
        if instrument in {item.symbol, f"{item.symbol}-PERP"}
    )
    if len(matches) != 1:
        raise StateConflict("durable instrument is not unique in fresh metadata")
    return matches[0]


def _recovery_source_hash(action: RecoveryAction) -> str:
    if isinstance(action, ReduceOnlyCloseAction):
        return action.position_snapshot_hash
    if isinstance(action, CancelByCloidAction):
        return action.account_snapshot_hash
    if isinstance(action, NoopFenceAction):
        return action.ambiguous_attempt_hash
    raise TypeError("action must be a typed recovery")


def _bounded_wire_price(
    value: Decimal,
    *,
    side: PositionSide,
    sz_decimals: int,
) -> Decimal:
    """Round only toward the book so tick encoding cannot widen slippage."""

    if not isinstance(value, Decimal) or not value.is_finite() or value <= _ZERO:
        raise ValidationError("raw recovery price bound is invalid")
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 6:
        raise ValidationError("recovery instrument szDecimals is invalid")
    decimal_exponent = -(6 - sz_decimals)
    significant_exponent = value.adjusted() - 4
    quantum = Decimal(1).scaleb(max(decimal_exponent, significant_exponent))
    rounding = ROUND_CEILING if side is PositionSide.LONG else ROUND_FLOOR
    try:
        with localcontext(_ARITHMETIC):
            result = value.quantize(quantum, rounding=rounding)
    except DecimalException as error:
        raise ValidationError("recovery price could not be tick-rounded") from error
    # This is also an independent assertion that the derived quantum matches
    # the venue encoder rather than a caller-controlled display convention.
    format_perp_price(result, sz_decimals=sz_decimals)
    if side is PositionSide.LONG and result < value:
        raise ValidationError("long close rounding widened adverse slippage")
    if side is PositionSide.SHORT and result > value:
        raise ValidationError("short close rounding widened adverse slippage")
    return result


class TestnetAccountSafetyController:
    """Prepare and durably queue at most one account-reducing recovery."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        signer_policy: SignerPolicy,
        recovery_authority: TestnetRecoveryAuthority,
        policy: SafetyControllerPolicy = SafetyControllerPolicy(),
    ) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be the exact ExecutionStore implementation")
        if store.environment is not Environment.TESTNET:
            raise ValidationError("account safety controller is testnet-only")
        verified_policy = _verified_policy(signer_policy)
        if not isinstance(recovery_authority, TestnetRecoveryAuthority):
            raise TypeError("recovery_authority must be TestnetRecoveryAuthority")
        if not isinstance(policy, SafetyControllerPolicy):
            raise TypeError("policy must be SafetyControllerPolicy")
        account = verified_policy.account(store.account_id)
        if HyperliquidNetwork.TESTNET not in verified_policy.allowed_networks:
            raise ValidationError("signer policy does not allow TESTNET")
        required = {
            RecoveryKind.REDUCE_ONLY_CLOSE,
            RecoveryKind.CANCEL_BY_CLOID,
            RecoveryKind.NOOP_FENCE,
        }
        if not required.issubset(verified_policy.allowed_recovery_kinds):
            raise ValidationError("signer policy lacks a required safety recovery kind")
        if policy.action_ttl_ms > verified_policy.maximum_expiry_horizon_ms:
            raise ValidationError("safety action TTL exceeds signer policy")
        if policy.action_ttl_ms < verified_policy.minimum_expiry_remaining_ms:
            raise ValidationError("safety action TTL is below signer policy minimum")
        self.store = store
        self.signer_policy = verified_policy
        self.signing_account = account
        self.recovery_authority = recovery_authority
        self.policy = policy
        self.safety_policy_hash = verified_policy.safety_policy_hash
        self._lock_path = Path(f"{store.path}.account-safety.lock")

    @contextmanager
    def _account_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _result(
        self,
        state: SafetyControllerState,
        reason: str,
        at: datetime,
        *,
        command: RecoveryCommand | None = None,
        prepared: PreparedRecovery | None = None,
    ) -> SafetyControllerResult:
        return SafetyControllerResult(
            state=state,
            reason_code=reason,
            observed_at=at,
            safety_policy_hash=self.safety_policy_hash,
            recovery_command=command,
            prepared=prepared,
        )

    def _snapshot_halt_reason(
        self,
        snapshot: HyperliquidAccountSnapshot,
        at: datetime,
    ) -> str | None:
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        if snapshot.network != "testnet":
            return "ACCOUNT_NETWORK_NOT_TESTNET"
        if snapshot.source_url != public_info_endpoint("testnet"):
            return "ACCOUNT_SOURCE_NOT_ALLOWLISTED"
        if snapshot.main_account_address != self.signing_account.main_account_address:
            return "ACCOUNT_ADDRESS_MISMATCH"
        try:
            _verify_snapshot_hash(snapshot)
        except (StateConflict, ValidationError):
            return "ACCOUNT_SNAPSHOT_INTEGRITY_FAILURE"
        at_ms = _milliseconds(at)
        if (
            snapshot.received_at_ms < snapshot.server_time_ms
            or snapshot.age_ms != snapshot.received_at_ms - snapshot.server_time_ms
            or snapshot.received_at_ms > at_ms + self.policy.future_skew_ms
        ):
            return "ACCOUNT_SNAPSHOT_PROVENANCE_INVALID"
        age = at_ms - snapshot.server_time_ms
        if (
            age > self.policy.max_account_snapshot_age_ms
            or age < -self.policy.future_skew_ms
        ):
            return "ACCOUNT_SNAPSHOT_STALE_OR_FUTURE"
        if not snapshot.positions and (
            snapshot.margin_summary.total_notional_position != _ZERO
            or snapshot.margin_summary.total_margin_used != _ZERO
        ):
            return "ACCOUNT_FLAT_POSITION_SUMMARY_CONTRADICTION"
        return None

    def _store_view(
        self,
        snapshot: HyperliquidAccountSnapshot,
        at: datetime,
    ) -> _StoreView:
        commands = _unique(self.store.list_commands(), "command_id", CommandRecord)
        outboxes = _unique(self.store.list_outboxes(), "command_id", OutboxRecord)
        incidents = _unique(self.store.list_incidents(), "incident_id", IncidentRecord)
        recoveries = _unique(
            self.store.list_recovery_commands(),
            "recovery_command_id",
            RecoveryCommand,
        )
        recovery_outboxes = _unique(
            self.store.list_recovery_outboxes(),
            "recovery_command_id",
            RecoveryOutbox,
        )
        positions = self.store.list_positions()
        protections = self.store.list_protections()
        if any(type(item) is not PositionRecord for item in positions):
            raise StorageError("execution store returned invalid position records")
        if any(type(item) is not ProtectionRecord for item in protections):
            raise StorageError("execution store returned invalid protection records")
        if set(commands) != set(outboxes):
            raise StorageError("command and outbox views disagree")
        if set(recoveries) != set(recovery_outboxes):
            raise StorageError("recovery command and outbox views disagree")
        for command_id, command in commands.items():
            if (
                command.state not in _COMMAND_STATES
                or outboxes[command_id].state != command.state
            ):
                raise StorageError("command and outbox state disagree")
        for recovery_id, recovery in recoveries.items():
            if (
                recovery.state not in _RECOVERY_STATES
                or recovery_outboxes[recovery_id].state != recovery.state
            ):
                raise StorageError("recovery command and outbox state disagree")
            if recovery.parent_command_id not in commands:
                raise StorageError("recovery references an unknown parent command")
            incident = incidents.get(recovery.incident_id)
            if incident is None or incident.command_id != recovery.parent_command_id:
                raise StorageError("recovery references an unbound incident")
        for incident in incidents.values():
            if incident.command_id is not None and incident.command_id not in commands:
                raise StorageError("incident references an unknown command")
            if incident.updated_at > at:
                raise StateConflict("incident is future-dated")
        server_at = _EPOCH + timedelta(milliseconds=snapshot.server_time_ms)
        if any(item.observed_at > server_at for item in positions + protections):
            raise StateConflict("account snapshot predates durable account truth")

        command_views: dict[str, _CommandView] = {}
        for command_id, command in commands.items():
            plan = self.store.get_plan_payload(command.plan_hash)
            entry = _mapping(plan.get("entry"), "durable plan entry")
            if (
                entry.get("environment") != "testnet"
                or entry.get("account_id") != self.store.account_id
                or entry.get("venue") != "hyperliquid"
            ):
                raise StateConflict("durable plan is outside controller scope")
            symbol = _symbol_from_instrument(snapshot, entry.get("instrument"))
            entry_side = entry.get("side")
            if entry_side not in {"buy", "sell"}:
                raise StorageError("durable plan entry side is invalid")
            legs = self.store.get_legs(command_id)
            if tuple(item.role for item in legs) != (
                "entry",
                "protective_stop",
                "take_profit",
            ):
                raise StorageError("durable command lacks its exact protected legs")
            by_role = {item.role: item for item in legs}
            if by_role["entry"].side != entry_side:
                raise StorageError("durable plan and leg entry sides differ")
            command_views[command_id] = _CommandView(
                command=command,
                outbox=outboxes[command_id],
                plan=plan,
                legs=legs,
                symbol=symbol,
                entry_side=entry_side,
                stop_cloid=by_role["protective_stop"].cloid,
                cloids=frozenset(item.cloid for item in legs),
            )
        return _StoreView(
            commands=command_views,
            incidents=incidents,
            recoveries=recoveries,
            recovery_outboxes=recovery_outboxes,
            positions=positions,
            protections=protections,
        )

    def _active_recovery(
        self,
        view: _StoreView,
        snapshot: HyperliquidAccountSnapshot,
        at: datetime,
    ) -> SafetyControllerResult | None:
        active = tuple(
            recovery for recovery in view.recoveries.values() if recovery.state != "terminal"
        )
        if len(active) > 1:
            return self._result(
                SafetyControllerState.HALTED,
                "MULTIPLE_ACTIVE_RECOVERIES",
                at,
            )
        if not active:
            return None
        command = active[0]
        outbox = view.recovery_outboxes[command.recovery_command_id]
        try:
            raw = json.loads(command.recovery_material_json)
            action = recovery_action_from_material(raw)
        except (TypeError, ValueError, ValidationError):
            return self._result(
                SafetyControllerState.HALTED,
                "ACTIVE_RECOVERY_MATERIAL_INVALID",
                at,
            )
        if (
            command.safety_policy_hash != self.safety_policy_hash
            or command.recovery_hash != action.recovery_hash
            or command.source_hash != _recovery_source_hash(action)
            or command.kind != action.kind.value
            or command.incident_id != action.incident_id
            or action.account_id != self.store.account_id
            or action.main_account_address != self.signing_account.main_account_address
            or action.network is not HyperliquidNetwork.TESTNET
            or action.kind not in self.signer_policy.allowed_recovery_kinds
        ):
            return self._result(
                SafetyControllerState.HALTED,
                "ACTIVE_RECOVERY_SCOPE_INVALID",
                at,
            )
        incident = view.incidents.get(command.incident_id)
        if (
            incident is None
            or incident.command_id != command.parent_command_id
            or incident.state != "open"
            or incident.severity != "critical"
        ):
            return self._result(
                SafetyControllerState.HALTED,
                "ACTIVE_RECOVERY_INCIDENT_INVALID",
                at,
            )
        action_cloids: set[str] = set()
        action_assets: set[int] = set()
        if isinstance(action, ReduceOnlyCloseAction):
            action_cloids.add(action.cloid)
            action_assets.add(action.asset_id)
        elif isinstance(action, CancelByCloidAction):
            action_cloids.update(item.cloid for item in action.requests)
            action_assets.update(action.asset_ids)
        parent = view.commands.get(command.parent_command_id)
        cloids_allowed = (
            action_cloids
            == {
                derive_recovery_close_cloid(
                    account_id=action.account_id,
                    incident_id=action.incident_id,
                    position_snapshot_hash=action.position_snapshot_hash,
                )
            }
            if isinstance(action, ReduceOnlyCloseAction)
            else (
                parent is not None and action_cloids.issubset(parent.cloids)
                if isinstance(action, CancelByCloidAction)
                else not action_cloids
            )
        )
        if not cloids_allowed or not action_assets.issubset(
            self.signer_policy.allowed_asset_ids
        ):
            return self._result(
                SafetyControllerState.HALTED,
                "ACTIVE_RECOVERY_ALLOWLIST_INVALID",
                at,
            )

        at_ms = _milliseconds(at)
        permit_expires_ms = _milliseconds(command.created_at) + (
            self.policy.permit_ttl_seconds * 1_000
        )
        expired = at_ms >= min(action.expires_at_ms, permit_expires_ms)
        lease_inactive = (
            outbox.lease_expires_at is None or outbox.lease_expires_at <= at
        )
        if expired and outbox.attempt_count == 0 and (
            outbox.state == "queued"
            or (outbox.state == "claimed" and lease_inactive)
        ):
            claimed = self.store.claim_next_recovery(
                "account-safety-expiry-normalizer",
                at=at,
                lease_seconds=5,
            )
            if claimed is not None:
                return self._result(
                    SafetyControllerState.HALTED,
                    "EXPIRED_RECOVERY_NORMALIZATION_RACE",
                    at,
                )
            return None

        prepared: PreparedRecovery | None = None
        if command.state == "queued" and not expired:
            if isinstance(action, NoopFenceAction):
                try:
                    evidence = self.store.get_attempt(command.parent_command_id)
                except RecordNotFound:
                    evidence = None
                if (
                    isinstance(evidence, AttemptRecord)
                    and evidence.state == "unknown"
                    and action.attempt_id == evidence.attempt_id
                    and action.original_nonce == evidence.nonce
                    and action.preflight_hash == evidence.preflight_hash
                    and action.signed_evidence_hash == evidence.signed_evidence_hash
                    and action.transport_evidence_hash
                    == evidence.transport_evidence_hash
                    and action.original_action_hash == evidence.action_hash
                    and action.original_wire_hash == evidence.wire_hash
                    and action.ambiguous_attempt_hash
                    == ambiguous_attempt_hash(evidence)
                ):
                    prepared = PreparedRecovery(action=action, evidence=evidence)
            else:
                expected_hash = (
                    action.position_snapshot_hash
                    if isinstance(action, ReduceOnlyCloseAction)
                    else action.account_snapshot_hash
                )
                if snapshot.snapshot_hash == expected_hash:
                    prepared = PreparedRecovery(action=action, evidence=snapshot)
        return self._result(
            SafetyControllerState.ACTIVE,
            "RECOVERY_ALREADY_ACTIVE",
            at,
            command=command,
            prepared=prepared,
        )

    def _market_prices(
        self,
        market: Mapping[str, Any] | None,
        *,
        symbol: str,
        close_size: Decimal,
        side: PositionSide,
        at: datetime,
    ) -> tuple[Decimal, Decimal]:
        root = _mapping(market, "market_brief")
        if (
            root.get("schema_version") != "hyperliquid.market_brief.v1"
            or root.get("venue") != "hyperliquid"
            or root.get("network") != "testnet"
            or root.get("symbol") != symbol
        ):
            raise ValidationError("market brief scope is invalid")
        sources = root.get("sources")
        endpoint = public_info_endpoint("testnet")
        expected_sources = {
            (endpoint, "/info", "metaAndAssetCtxs"),
            (endpoint, "/info", "l2Book"),
        }
        if not isinstance(sources, list) or {
            (
                item.get("url"),
                item.get("endpoint"),
                item.get("request_type"),
            )
            for item in sources
            if isinstance(item, Mapping)
        } != expected_sources or len(sources) != 2:
            raise ValidationError("market brief sources are not exact allowlisted reads")
        book = _mapping(root.get("book"), "market_brief.book")
        time_ms = book.get("time_ms")
        age_ms = root.get("age_ms")
        if type(time_ms) is not int or type(age_ms) is not int or age_ms < 0:
            raise ValidationError("market brief time fields are invalid")
        observed_age = _milliseconds(at) - time_ms
        if (
            age_ms > self.policy.max_market_age_ms
            or observed_age > self.policy.max_market_age_ms
            or observed_age < -self.policy.future_skew_ms
        ):
            raise ValidationError("market brief is stale or future-dated")
        best_bid = _decimal_string(book.get("best_bid"), "book.best_bid", positive=True)
        best_ask = _decimal_string(book.get("best_ask"), "book.best_ask", positive=True)
        midpoint = _decimal_string(book.get("mid"), "book.mid", positive=True)
        if not best_bid < midpoint < best_ask:
            raise ValidationError("market brief book is crossed, locked, or inconsistent")
        consistency = _mapping(root.get("mid_consistency"), "mid_consistency")
        if consistency.get("within_limit") is not True:
            raise ValidationError("market context and book are inconsistent")
        depth = _mapping(book.get("depth"), "book.depth")
        band = next(
            value
            for value in (5, 10, 25)
            if self.policy.max_flatten_slippage_bps <= Decimal(value)
        )
        selected_depth = _mapping(depth.get(f"{band}bps"), f"book.depth.{band}bps")
        depth_field = "bid_size" if side is PositionSide.LONG else "ask_size"
        available = _decimal_string(
            selected_depth.get(depth_field),
            f"book.depth.{band}bps.{depth_field}",
        )
        if available < close_size:
            raise ValidationError("market depth is insufficient for bounded full flatten")
        return best_bid, best_ask

    def _unknown_candidate(
        self,
        view: _StoreView,
        snapshot: HyperliquidAccountSnapshot,
    ) -> tuple[IncidentRecord, AttemptRecord] | None:
        if snapshot.positions or snapshot.all_open_orders():
            return None
        candidates: list[tuple[IncidentRecord, AttemptRecord]] = []
        for incident in view.incidents.values():
            if (
                incident.state != "open"
                or incident.severity != "critical"
                or incident.command_id is None
                or incident.code != "UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING"
            ):
                continue
            try:
                attempt = self.store.get_attempt(incident.command_id)
            except RecordNotFound:
                continue
            prior_noops = tuple(
                recovery
                for recovery in view.recoveries.values()
                if recovery.parent_command_id == incident.command_id
                and recovery.kind == RecoveryKind.NOOP_FENCE.value
            )
            if any(
                self._noop_suppresses_replacement(
                    recovery,
                    view.recovery_outboxes[recovery.recovery_command_id],
                )
                for recovery in prior_noops
            ):
                continue
            if attempt.state == "unknown":
                candidates.append((incident, attempt))
        command_ids = {attempt.command_id for _, attempt in candidates}
        if len(command_ids) > 1:
            raise StateConflict("multiple unknown parent attempts require manual halt")
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0].opened_at, item[0].incident_id))

    def _noop_suppresses_replacement(
        self,
        recovery: RecoveryCommand,
        outbox: RecoveryOutbox,
    ) -> bool:
        """Return false only when a terminal noop is proven never submitted."""

        if recovery.state != "terminal":
            return True
        if outbox.attempt_count == 0:
            return outbox.current_attempt_id is not None
        if outbox.attempt_count != 1 or outbox.current_attempt_id is None:
            return True
        try:
            attempt = self.store.get_recovery_attempt(
                recovery.recovery_command_id
            )
        except RecordNotFound:
            return True
        if attempt.attempt_id != outbox.current_attempt_id:
            return True
        # `require_recovery_submission_authority` atomically changes prepared
        # to sending.  Therefore prepared + no transport is the exact durable
        # proof that no venue write was authorized; every later state must
        # suppress a second same-nonce noop.
        if attempt.state != "prepared" or attempt.transport_evidence_hash is not None:
            return True
        try:
            self.store.get_recovery_transport_evidence(
                recovery.recovery_command_id
            )
        except RecordNotFound:
            return False
        return True

    def _derived_recovery_cloid(
        self,
        view: _StoreView,
        snapshot: HyperliquidAccountSnapshot,
        incident: IncidentRecord,
    ) -> str:
        derived = derive_recovery_close_cloid(
            account_id=self.store.account_id,
            incident_id=incident.incident_id,
            position_snapshot_hash=snapshot.snapshot_hash,
        )
        used: set[str] = set()
        for command in view.commands.values():
            used.update(command.cloids)
        used.update(
            order.cloid
            for order in snapshot.all_open_orders()
            if order.cloid is not None
        )
        for recovery in view.recoveries.values():
            if recovery.kind != RecoveryKind.REDUCE_ONLY_CLOSE.value:
                continue
            try:
                action = recovery_action_from_material(
                    json.loads(recovery.recovery_material_json)
                )
            except (TypeError, ValueError, ValidationError) as error:
                raise StateConflict("durable recovery material is invalid") from error
            if isinstance(action, ReduceOnlyCloseAction):
                used.add(action.cloid)
        if derived in used:
            raise StateConflict(
                "derived recovery close CLOID collides with durable account state"
            )
        return derived

    def _queue(
        self,
        action: RecoveryAction,
        evidence: HyperliquidAccountSnapshot | AttemptRecord,
        incident: IncidentRecord,
        at: datetime,
    ) -> SafetyControllerResult:
        identifier = action.recovery_hash
        permit_id = f"safety-permit-{identifier}"
        command_id = f"safety-recovery-{identifier}"
        authorization = self.recovery_authority.issue(
            action,
            incident,
            permit_id=permit_id,
            safety_policy_hash=self.safety_policy_hash,
            at=at,
            ttl_seconds=self.policy.permit_ttl_seconds,
        )
        permit = verified_recovery_permit(
            self.recovery_authority,
            authorization,
            action,
            incident,
            safety_policy_hash=self.safety_policy_hash,
            at=at,
        )
        self.store.register_recovery_permit(permit)
        try:
            command = self.store.queue_recovery(
                recovery_command_id=command_id,
                permit_id=permit.permit_id,
                token_hash=permit.token_hash,
                audience=permit.audience,
                at=at,
            )
        except (AdmissionDenied, StateConflict):
            active = self.store.list_recovery_commands(active_only=True)
            exact = tuple(
                item
                for item in active
                if item.recovery_command_id == command_id
                and item.recovery_hash == action.recovery_hash
                and item.source_hash == _recovery_source_hash(action)
            )
            if len(exact) != 1:
                raise
            return self._result(
                SafetyControllerState.ACTIVE,
                "RECOVERY_ALREADY_ACTIVE",
                at,
                command=exact[0],
                prepared=PreparedRecovery(action=action, evidence=evidence),
            )
        return self._result(
            SafetyControllerState.QUEUED,
            f"{action.kind.value.upper()}_QUEUED",
            at,
            command=command,
            prepared=PreparedRecovery(action=action, evidence=evidence),
        )

    def _evaluate_locked(
        self,
        snapshot: HyperliquidAccountSnapshot,
        market_brief: Mapping[str, Any] | None,
        at: datetime,
    ) -> SafetyControllerResult:
        halt = self._snapshot_halt_reason(snapshot, at)
        if halt is not None:
            return self._result(SafetyControllerState.HALTED, halt, at)
        try:
            view = self._store_view(snapshot, at)
        except (RecordNotFound, StateConflict, StorageError, ValidationError):
            return self._result(
                SafetyControllerState.HALTED,
                "DURABLE_STATE_INVALID_OR_STALE",
                at,
            )

        active = self._active_recovery(view, snapshot, at)
        if active is not None:
            return active
        # Expiry normalization may have terminalized the sole active command.
        # Re-read every verified view before creating replacement authority.
        if any(item.state != "terminal" for item in view.recoveries.values()):
            view = self._store_view(snapshot, at)
            active = self._active_recovery(view, snapshot, at)
            if active is not None:
                return active

        try:
            unknown = self._unknown_candidate(view, snapshot)
        except StateConflict:
            return self._result(
                SafetyControllerState.HALTED,
                "MULTIPLE_UNKNOWN_PARENT_ATTEMPTS",
                at,
            )
        if unknown is not None:
            incident, attempt = unknown
            try:
                action = build_noop_fence(
                    attempt,
                    incident=incident,
                    account_id=self.store.account_id,
                    main_account_address=self.signing_account.main_account_address,
                    network=HyperliquidNetwork.TESTNET,
                    at=at,
                    ttl_ms=self.policy.action_ttl_ms,
                )
                return self._queue(action, attempt, incident, at)
            except (AdmissionDenied, RecordNotFound, StateConflict, ValidationError):
                return self._result(
                    SafetyControllerState.HALTED,
                    "NOOP_FENCE_AUTHORITY_REJECTED",
                    at,
                )

        open_orders = snapshot.all_open_orders()
        known_plan_cloids = frozenset(
            cloid for command in view.commands.values() for cloid in command.cloids
        )
        foreign_orders = tuple(
            item for item in open_orders
            if item.cloid is None or item.cloid not in known_plan_cloids
        )
        if foreign_orders:
            return self._result(
                SafetyControllerState.HALTED,
                "FOREIGN_OR_UNOWNED_OPEN_ORDER",
                at,
            )
        if any(item.cloid not in known_plan_cloids for item in open_orders):
            return self._result(
                SafetyControllerState.HALTED,
                "UNBOUND_OWNED_OPEN_ORDER",
                at,
            )
        if len({item.cloid for item in open_orders}) != len(open_orders):
            return self._result(
                SafetyControllerState.HALTED,
                "DUPLICATE_OPEN_ORDER_CLOID",
                at,
            )
        if any(item.asset_id not in self.signer_policy.allowed_asset_ids for item in snapshot.positions):
            return self._result(
                SafetyControllerState.HALTED,
                "FOREIGN_OPEN_POSITION",
                at,
            )
        if len(snapshot.positions) > 1:
            return self._result(
                SafetyControllerState.HALTED,
                "MULTIPLE_OPEN_POSITIONS_REQUIRE_SEPARATE_BRIEFS",
                at,
            )

        open_critical = tuple(
            item
            for item in view.incidents.values()
            if item.state == "open" and item.severity == "critical"
        )
        if any(item.command_id is None for item in open_critical):
            return self._result(
                SafetyControllerState.HALTED,
                "UNBOUND_CRITICAL_INCIDENT",
                at,
            )

        if snapshot.positions:
            position = snapshot.positions[0]
            candidates: list[tuple[IncidentRecord, _CommandView, bool, bool]] = []
            for incident in open_critical:
                assert incident.command_id is not None
                command = view.commands[incident.command_id]
                if command.symbol != position.symbol:
                    continue
                coverage = snapshot.protection_coverage(
                    position.symbol,
                    expected_stop_cloids=(command.stop_cloid,),
                )
                exact_protection = coverage.covered_size == position.absolute_size
                direction_matches = (
                    position.side is PositionSide.LONG and command.entry_side == "buy"
                ) or (
                    position.side is PositionSide.SHORT and command.entry_side == "sell"
                )
                if not exact_protection or not direction_matches:
                    candidates.append(
                        (incident, command, exact_protection, direction_matches)
                    )
            if not candidates:
                return self._result(
                    SafetyControllerState.HALTED,
                    "UNSAFE_POSITION_HAS_NO_BOUND_CRITICAL_INCIDENT",
                    at,
                )
            supported = tuple(
                item for item in candidates if item[0].code in _RECOVERABLE_INCIDENT_CODES
            )
            if not supported:
                return self._result(
                    SafetyControllerState.HALTED,
                    "POSITION_INCIDENT_CODE_NOT_AUTOMATABLE",
                    at,
                )
            parent_ids = {item[1].command.command_id for item in supported}
            if len(parent_ids) != 1:
                return self._result(
                    SafetyControllerState.HALTED,
                    "POSITION_OWNERSHIP_AMBIGUOUS",
                    at,
                )
            incident, command, _, _ = min(
                supported,
                key=lambda item: (
                    0 if item[0].code == "POSITION_DIRECTION_CONTRADICTION" else 1,
                    item[0].opened_at,
                    item[0].incident_id,
                ),
            )
            if any(not item.reduce_only for item in open_orders):
                return self._result(
                    SafetyControllerState.HALTED,
                    "EXPOSURE_INCREASING_ORDER_OPEN_DURING_RECOVERY",
                    at,
                )
            try:
                best_bid, best_ask = self._market_prices(
                    market_brief,
                    symbol=position.symbol,
                    close_size=position.absolute_size,
                    side=position.side,
                    at=at,
                )
                with localcontext(_ARITHMETIC) as context:
                    slippage = context.divide(
                        self.policy.max_flatten_slippage_bps,
                        _BASIS_POINTS,
                    )
                    if position.side is PositionSide.LONG:
                        price_bound = context.multiply(
                            best_bid,
                            context.subtract(Decimal("1"), slippage),
                        )
                    else:
                        price_bound = context.multiply(
                            best_ask,
                            context.add(Decimal("1"), slippage),
                        )
                price_bound = _bounded_wire_price(
                    price_bound,
                    side=position.side,
                    sz_decimals=snapshot.metadata.instrument(position.symbol).sz_decimals,
                )
                close_cloid = self._derived_recovery_cloid(
                    view,
                    snapshot,
                    incident,
                )
                action = build_reduce_only_close(
                    snapshot,
                    symbol=position.symbol,
                    price_bound=price_bound,
                    cloid=close_cloid,
                    incident=incident,
                    account_id=self.store.account_id,
                    network=HyperliquidNetwork.TESTNET,
                    at=at,
                    close_size=position.absolute_size,
                    ttl_ms=self.policy.action_ttl_ms,
                )
                return self._queue(action, snapshot, incident, at)
            except (
                AdmissionDenied,
                DecimalException,
                RecordNotFound,
                StateConflict,
                ValidationError,
            ):
                return self._result(
                    SafetyControllerState.HALTED,
                    "BOUNDED_FLATTEN_PREPARATION_REJECTED",
                    at,
                )

        if open_orders:
            order_cloids = frozenset(item.cloid for item in open_orders)
            candidates = tuple(
                (
                    incident,
                    view.commands[incident.command_id],
                )
                for incident in open_critical
                if incident.command_id is not None
                and incident.code in _RECOVERABLE_INCIDENT_CODES
                and order_cloids.issubset(view.commands[incident.command_id].cloids)
            )
            if not candidates:
                return self._result(
                    SafetyControllerState.HALTED,
                    "FLAT_ORDER_CLEANUP_HAS_NO_BOUND_CRITICAL_INCIDENT",
                    at,
                )
            parent_ids = {item[1].command.command_id for item in candidates}
            if len(parent_ids) != 1 or len(open_orders) > 20:
                return self._result(
                    SafetyControllerState.HALTED,
                    "FLAT_ORDER_CLEANUP_SCOPE_AMBIGUOUS",
                    at,
                )
            incident, _ = min(
                candidates,
                key=lambda item: (item[0].opened_at, item[0].incident_id),
            )
            requests = tuple(
                CancelRequest(item.symbol, item.cloid)
                for item in sorted(open_orders, key=lambda value: (value.symbol, value.cloid))
                if item.cloid is not None
            )
            try:
                action = build_cancel_by_cloid(
                    snapshot,
                    requests,
                    owned_cloids=known_plan_cloids,
                    incident=incident,
                    account_id=self.store.account_id,
                    network=HyperliquidNetwork.TESTNET,
                    at=at,
                    ttl_ms=self.policy.action_ttl_ms,
                )
                return self._queue(action, snapshot, incident, at)
            except (AdmissionDenied, RecordNotFound, StateConflict, ValidationError):
                return self._result(
                    SafetyControllerState.HALTED,
                    "FLAT_CANCEL_PREPARATION_REJECTED",
                    at,
                )

        if open_critical:
            return self._result(
                SafetyControllerState.HALTED,
                "CRITICAL_INCIDENT_REQUIRES_RECONCILIATION",
                at,
            )
        return self._result(SafetyControllerState.SAFE, "ACCOUNT_SAFE", at)

    def evaluate(
        self,
        snapshot: HyperliquidAccountSnapshot,
        market_brief: Mapping[str, Any] | None,
        *,
        at: datetime,
    ) -> SafetyControllerResult:
        """Inspect current truth and queue at most one short-lived recovery.

        ``market_brief`` is required only when a position must be flattened;
        same-nonce fencing and globally-flat cancellation do not consume a
        price.  Runtime state failures are returned as ``HALTED`` without
        authority, while programmer type errors remain explicit exceptions.
        """

        checked_at = _utc(at, "at")
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        if market_brief is not None and not isinstance(market_brief, Mapping):
            raise TypeError("market_brief must be a mapping or None")
        with self._account_lock():
            try:
                return self._evaluate_locked(snapshot, market_brief, checked_at)
            except (AdmissionDenied, RecordNotFound, StateConflict, StorageError, ValidationError):
                return self._result(
                    SafetyControllerState.HALTED,
                    "SAFETY_CONTROLLER_FAIL_CLOSED",
                    checked_at,
                )


__all__ = (
    "SafetyControllerPolicy",
    "SafetyControllerResult",
    "SafetyControllerState",
    "TestnetAccountSafetyController",
)
