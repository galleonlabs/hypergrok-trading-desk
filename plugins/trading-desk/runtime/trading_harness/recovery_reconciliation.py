"""Derive recovery reconciliation proof from typed venue truth.

The coordinator never signs or submits.  It claims an already-submitted
recovery command, checks its immutable action material against a fresh
Hyperliquid account snapshot and typed order/fill evidence, derives the only
``RecoveryReconciliationProof`` accepted by the execution store, and records
that proof.  Callers cannot supply success, completeness, position, protection
or affected-CLOID booleans.

Same-nonce noop reconciliation intentionally remains fail-closed: the current
store persists a response hash but not the canonical noop response body needed
to prove fence acceptance after restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Callable, Iterable

from .canonical import domain_hash
from .errors import RecordNotFound, StateConflict, ValidationError
from .execution_store import (
    ExecutionStore,
    RecoveryCommand,
    RecoveryReconciliationProof,
    RecoveryVenueFill,
    TransportOutcomeEvidence,
)
from .hyperliquid_account import HyperliquidAccountSnapshot, OrderSide
from .hyperliquid_reconcile import (
    FILL_LOOKBACK_MS,
    AuxiliaryFillEvidence,
    AuxiliaryOrderEvidence,
    AuxiliaryOwnedOrder,
    FillCoverage,
    ParsedOrderStatus,
    SignedFillEvidence,
    VenueOrderState,
    canonical_hyperliquid_fill_id,
)
from .market_data import public_info_endpoint
from .policy import exact_decimal
from .reconciliation_coordinator import _owned_legs, _verify_snapshot_hash


RECOVERY_VENUE_READ_HASH_DOMAIN = (
    "trading-harness/hyperliquid-recovery-venue-read/v1"
)
_MAX_SNAPSHOT_AGE = timedelta(seconds=5)
_LATE_WRITE_SETTLEMENT_MS = 5_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO = Decimal("0")
_DEFINITIVE_ORDER_STATUSES = frozenset(
    {
        "filled",
        "canceled",
        "rejected",
        "marginCanceled",
        "vaultWithdrawalCanceled",
        "openInterestCapCanceled",
        "selfTradeCanceled",
        "reduceOnlyCanceled",
        "siblingFilledCanceled",
        "delistedCanceled",
        "liquidatedCanceled",
        "scheduledCancel",
        "tickRejected",
        "minTradeNtlRejected",
        "perpMarginRejected",
        "reduceOnlyRejected",
        "badAloPxRejected",
        "iocCancelRejected",
        "badTriggerPxRejected",
        "marketOrderNoLiquidityRejected",
        "positionIncreaseAtOpenInterestCapRejected",
        "positionFlipAtOpenInterestCapRejected",
        "tooAggressiveAtOpenInterestCapRejected",
        "openInterestIncreaseRejected",
        "insufficientSpotBalanceRejected",
        "oracleRejected",
        "perpMaxPositionRejected",
    }
)


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RecoveryVenueRead:
    """Typed result of allowlisted order-status/fill reads for one recovery."""

    network: str
    account_id: str
    account_snapshot_hash: str
    observed_at: datetime
    order_statuses: tuple[ParsedOrderStatus, ...]
    signed_fills: tuple[SignedFillEvidence, ...]
    fill_coverage: FillCoverage
    evidence_hash: str = ""
    auxiliary_order_statuses: tuple[AuxiliaryOrderEvidence, ...] = ()
    auxiliary_fills: tuple[AuxiliaryFillEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.network != "testnet":
            raise ValidationError("recovery venue read is testnet-only")
        if not isinstance(self.account_id, str) or not self.account_id:
            raise ValidationError("account_id is required")
        if (
            not isinstance(self.account_snapshot_hash, str)
            or len(self.account_snapshot_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.account_snapshot_hash
            )
        ):
            raise ValidationError("account_snapshot_hash is invalid")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        statuses = tuple(self.order_statuses)
        fills = tuple(self.signed_fills)
        auxiliary_statuses = tuple(self.auxiliary_order_statuses)
        auxiliary_fills = tuple(self.auxiliary_fills)
        if any(not isinstance(item, ParsedOrderStatus) for item in statuses):
            raise TypeError("order_statuses must contain ParsedOrderStatus")
        if any(not isinstance(item, SignedFillEvidence) for item in fills):
            raise TypeError("signed_fills must contain SignedFillEvidence")
        if any(
            not isinstance(item, AuxiliaryOrderEvidence)
            for item in auxiliary_statuses
        ):
            raise TypeError(
                "auxiliary_order_statuses must contain AuxiliaryOrderEvidence"
            )
        if any(
            not isinstance(item, AuxiliaryFillEvidence)
            for item in auxiliary_fills
        ):
            raise TypeError("auxiliary_fills must contain AuxiliaryFillEvidence")
        if not isinstance(self.fill_coverage, FillCoverage):
            raise TypeError("fill_coverage must be FillCoverage")
        requested = tuple(item.requested_cloid for item in statuses)
        if len(requested) != len(set(requested)):
            raise ValidationError("recovery venue read repeats a CLOID status")
        fill_ids = tuple(item.fill_id for item in fills)
        if len(fill_ids) != len(set(fill_ids)):
            raise ValidationError("recovery venue read repeats fill identity")
        all_fill_ids = [*fill_ids, *(item.fill.fill_id for item in auxiliary_fills)]
        if len(all_fill_ids) != len(set(all_fill_ids)):
            raise ValidationError("recovery venue read repeats cross-lane fill identity")
        if self.fill_coverage.unique_fills != (
            len(fills)
            + len(auxiliary_fills)
            + self.fill_coverage.unmatched_fills
        ):
            raise ValidationError("fill coverage count differs from signed fills")
        observed_ms = int(self.observed_at.timestamp() * 1_000)
        if any(
            item.status_timestamp_ms is not None
            and item.status_timestamp_ms > observed_ms
            for item in statuses
        ):
            raise ValidationError("order status is later than venue read cutoff")
        if any(
            item.time_ms > observed_ms
            for item in [*fills, *(value.fill for value in auxiliary_fills)]
        ):
            raise ValidationError("fill is later than venue read cutoff")
        object.__setattr__(self, "order_statuses", statuses)
        object.__setattr__(self, "signed_fills", fills)
        object.__setattr__(self, "auxiliary_order_statuses", auxiliary_statuses)
        object.__setattr__(self, "auxiliary_fills", auxiliary_fills)
        material = self.material()
        expected = domain_hash(RECOVERY_VENUE_READ_HASH_DOMAIN, material)
        if self.evidence_hash and self.evidence_hash != expected:
            raise ValidationError("recovery venue evidence hash differs")
        object.__setattr__(self, "evidence_hash", expected)

    @property
    def fill_chain_complete(self) -> bool:
        return (
            self.fill_coverage.complete
            and not self.fill_coverage.page_saturated
            and not self.fill_coverage.retention_limited
            and self.fill_coverage.unmatched_fills == 0
        )

    def material(self) -> dict[str, object]:
        return {
            "network": self.network,
            "account_id": self.account_id,
            "account_snapshot_hash": self.account_snapshot_hash,
            "observed_at": self.observed_at,
            "order_statuses": [item.canonical_record() for item in self.order_statuses],
            "signed_fills": [item.canonical_record() for item in self.signed_fills],
            "auxiliary_order_statuses": [
                item.canonical_record() for item in self.auxiliary_order_statuses
            ],
            "auxiliary_fills": [
                item.canonical_record() for item in self.auxiliary_fills
            ],
            "fill_coverage": self.fill_coverage.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RecoveryCoordinationResult:
    recovery_command_id: str
    recovery_state: str
    proof: RecoveryReconciliationProof
    incomplete_reasons: tuple[str, ...]
    incident_resolution: str | None
    required_schema_change: str | None = None


def _status_by_cloid(
    evidence: RecoveryVenueRead,
) -> dict[str, ParsedOrderStatus]:
    return {item.requested_cloid: item for item in evidence.order_statuses}


def _open_cloids(snapshot: HyperliquidAccountSnapshot) -> tuple[str, ...]:
    return tuple(
        sorted(
            order.cloid
            for order in snapshot.all_open_orders()
            if order.cloid is not None
        )
    )


def _position(snapshot: HyperliquidAccountSnapshot, symbol: str) -> Decimal:
    value = snapshot.position(symbol)
    return _ZERO if value is None else value.signed_size


def _definitive_status(status: ParsedOrderStatus | None) -> bool:
    if status is None:
        return False
    if status.state is VenueOrderState.MISSING:
        return True
    return status.venue_status in _DEFINITIVE_ORDER_STATUSES


def _fills_for(
    fills: Iterable[SignedFillEvidence], cloid: str
) -> tuple[SignedFillEvidence, ...]:
    return tuple(sorted(
        (item for item in fills if item.cloid == cloid),
        key=lambda item: (item.time_ms, item.tid, item.fill_id),
    ))


class RecoveryReconciliationCoordinator:
    """Claim and reconcile one recovery command using typed read-only evidence."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        lease_seconds: int = 15,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, ExecutionStore):
            raise TypeError("store must be ExecutionStore")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 60:
            raise ValidationError("lease_seconds must be from 5 to 60")
        if store.environment.value != "testnet":
            raise ValidationError("recovery coordinator is testnet-only")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        self.store = store
        self.lease_seconds = lease_seconds
        self.clock = clock

    def _mutation_time(self, fallback: datetime) -> datetime:
        if self.clock is None:
            return fallback
        try:
            return _utc(self.clock(), "recovery coordinator clock")
        except Exception as error:
            if isinstance(error, (TypeError, ValidationError)):
                raise
            raise ValidationError("recovery coordinator clock failed") from error

    def _validate_common(
        self,
        command: RecoveryCommand,
        transport: TransportOutcomeEvidence,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        at: datetime,
    ) -> tuple[datetime, dict[str, object]]:
        checked_at = _utc(at, "at")
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        if not isinstance(evidence, RecoveryVenueRead):
            raise TypeError("evidence must be RecoveryVenueRead")
        if snapshot.network != "testnet" or evidence.network != "testnet":
            raise ValidationError("recovery reconciliation is testnet-only")
        _verify_snapshot_hash(snapshot)
        if evidence.account_id != self.store.account_id:
            raise StateConflict("recovery venue evidence account differs from store")
        try:
            material = json.loads(command.recovery_material_json)
        except ValueError as error:
            raise StateConflict("persisted recovery material is invalid") from error
        if not isinstance(material, dict):
            raise StateConflict("persisted recovery material is not an object")
        main_account_address = material.get("main_account_address")
        if (
            not isinstance(main_account_address, str)
            or snapshot.main_account_address != main_account_address
        ):
            raise StateConflict(
                "account snapshot address differs from persisted recovery account"
            )
        if evidence.account_snapshot_hash != snapshot.snapshot_hash:
            raise StateConflict("recovery evidence and account snapshot hashes differ")
        if evidence.observed_at != checked_at:
            raise StateConflict("coordinator time differs from venue evidence cutoff")
        server_at = _EPOCH + timedelta(milliseconds=snapshot.server_time_ms)
        if not server_at <= checked_at <= server_at + _MAX_SNAPSHOT_AGE:
            raise StateConflict("recovery account snapshot is stale or future-dated")
        checked_at_ms = int(checked_at.timestamp() * 1_000)
        if (
            snapshot.source_url != public_info_endpoint("testnet")
            or snapshot.received_at_ms < snapshot.server_time_ms
            or snapshot.received_at_ms > checked_at_ms
            or snapshot.age_ms
            != snapshot.received_at_ms - snapshot.server_time_ms
        ):
            raise StateConflict("recovery account snapshot provenance is invalid")
        if any(
            status.status_timestamp_ms is not None
            and status.status_timestamp_ms > snapshot.server_time_ms
            for status in evidence.order_statuses
        ) or any(
            fill.time_ms > snapshot.server_time_ms
            for fill in [
                *evidence.signed_fills,
                *(item.fill for item in evidence.auxiliary_fills),
            ]
        ):
            raise StateConflict("recovery venue facts postdate account snapshot")
        if evidence.fill_coverage.requested_end_time_ms > snapshot.server_time_ms:
            raise StateConflict("fill coverage extends beyond account snapshot")
        if (
            evidence.fill_coverage.complete
            and evidence.fill_coverage.requested_end_time_ms
            != snapshot.server_time_ms
        ):
            raise StateConflict(
                "complete recovery fill coverage must end at account snapshot"
            )
        expected_auxiliary: dict[str, AuxiliaryOwnedOrder] = {}
        if command.kind == "reduce_only_close":
            parent = self.store.get_command(command.parent_command_id)
            expected_auxiliary = {
                item.role: AuxiliaryOwnedOrder(
                    owner_kind="parent_leg",
                    owner_id=command.parent_command_id,
                    source_hash=parent.plan_hash,
                    role=item.role,
                    cloid=item.cloid,
                    symbol=item.symbol,
                    side=item.side,
                    requested_quantity=item.requested_quantity,
                    is_trigger=item.role != "entry",
                    reduce_only=item.role != "entry",
                )
                for item in _owned_legs(
                    self.store, command.parent_command_id, snapshot
                )
            }
        observed_auxiliary = {
            item.order.role: item for item in evidence.auxiliary_order_statuses
        }
        if (
            len(observed_auxiliary) != len(evidence.auxiliary_order_statuses)
            or set(observed_auxiliary) != set(expected_auxiliary)
        ):
            raise StateConflict(
                "recovery auxiliary order set differs from durable parent"
            )
        auxiliary_by_oid: dict[int, AuxiliaryOrderEvidence] = {}
        for role, observed in observed_auxiliary.items():
            expected = expected_auxiliary[role]
            status = observed.status
            if observed.order != expected or status.requested_cloid != expected.cloid:
                raise StateConflict(
                    "recovery auxiliary status differs from durable parent"
                )
            if status.state is VenueOrderState.ORDER:
                if (
                    status.oid is None
                    or status.symbol != expected.symbol
                    or status.original_size != expected.requested_quantity
                    or status.is_trigger is not expected.is_trigger
                    or status.reduce_only is not expected.reduce_only
                    or status.oid in auxiliary_by_oid
                ):
                    raise StateConflict(
                        "recovery auxiliary parent order semantics differ"
                    )
                auxiliary_by_oid[status.oid] = observed
            elif any(
                value is not None
                for value in (
                    status.oid,
                    status.symbol,
                    status.original_size,
                    status.remaining_size,
                    status.is_trigger,
                    status.reduce_only,
                )
            ):
                raise StateConflict(
                    "missing recovery auxiliary status retains venue fields"
                )
        target_oids = {
            item.oid for item in evidence.order_statuses if item.oid is not None
        }
        if target_oids & set(auxiliary_by_oid):
            raise StateConflict("recovery target and parent share a venue OID")
        target_fill_ids = {item.fill_id for item in evidence.signed_fills}
        if len(target_fill_ids) != len(evidence.signed_fills):
            raise StateConflict("recovery target fills repeat identity")
        if any(
            item.fill_id != canonical_hyperliquid_fill_id(item)
            for item in evidence.signed_fills
        ):
            raise StateConflict("recovery target fill identity is not canonical")
        auxiliary_quantities: dict[str, Decimal] = {
            role: _ZERO for role in expected_auxiliary
        }
        for attributed in evidence.auxiliary_fills:
            fill = attributed.fill
            expected = expected_auxiliary.get(fill.role)
            status = auxiliary_by_oid.get(fill.oid)
            if (
                fill.fill_id != canonical_hyperliquid_fill_id(fill)
                or fill.fill_id in target_fill_ids
                or attributed.owner_kind != "parent_leg"
                or attributed.owner_id != command.parent_command_id
                or expected is None
                or attributed.source_hash != expected.source_hash
                or status is None
                or status.order != expected
                or fill.cloid != expected.cloid
                or fill.symbol != expected.symbol
                or fill.side is not expected.side
                or fill.quantity != abs(fill.signed_quantity)
                or fill.end_position != fill.start_position + fill.signed_quantity
            ):
                raise StateConflict(
                    "recovery auxiliary fill differs from durable parent"
                )
            target_fill_ids.add(fill.fill_id)
            auxiliary_quantities[fill.role] += fill.quantity
        if any(
            auxiliary_quantities[role] > expected.requested_quantity
            for role, expected in expected_auxiliary.items()
        ):
            raise StateConflict("recovery auxiliary fills exceed parent leg size")
        all_fills = sorted(
            [
                *evidence.signed_fills,
                *(item.fill for item in evidence.auxiliary_fills),
            ],
            key=lambda item: (item.time_ms, item.tid, item.oid),
        )
        if any(
            left.end_position != right.start_position
            for left, right in zip(all_fills, all_fills[1:])
        ):
            raise StateConflict("cross-lane recovery fill chain is discontinuous")
        if all_fills:
            symbols = {item.symbol for item in all_fills}
            if len(symbols) != 1:
                raise StateConflict("cross-lane recovery fills span symbols")
            if command.kind == "reduce_only_close":
                try:
                    original_position = exact_decimal(
                        material["original_signed_position"],
                        field="original_signed_position",
                    )
                except (KeyError, ValidationError) as error:
                    raise StateConflict(
                        "recovery close lacks original position binding"
                    ) from error
                exact_source_watermark = material.get(
                    "position_snapshot_time_ms"
                )
                source_anchored = (
                    all_fills[0].start_position == original_position
                    if type(exact_source_watermark) is int
                    else any(
                        item.start_position == original_position
                        for item in all_fills
                    )
                )
                if not source_anchored:
                    raise StateConflict(
                        "cross-lane recovery fill chain lacks its source position anchor"
                    )
            if all_fills[-1].end_position != _position(
                snapshot, next(iter(symbols))
            ):
                raise StateConflict(
                    "cross-lane recovery fill chain does not reach snapshot"
                )
        attempt = self.store.get_recovery_attempt(command.recovery_command_id)
        if command.kind == "reduce_only_close":
            source_watermark = material.get("position_snapshot_time_ms")
        elif command.kind == "cancel_by_cloid":
            source_watermark = material.get("account_snapshot_time_ms")
        else:
            source_watermark = self.store.get_preflight(
                command.parent_command_id
            ).account_server_time_ms
        expected_fill_start_ms = (
            max(
                0,
                int(attempt.prepared_at.timestamp() * 1_000)
                - FILL_LOOKBACK_MS,
            )
            if source_watermark is None
            else source_watermark
        )
        if type(expected_fill_start_ms) is not int or expected_fill_start_ms < 0:
            raise StateConflict(
                "recovery fill source watermark is invalid"
            )
        if (
            evidence.fill_coverage.requested_start_time_ms
            != expected_fill_start_ms
        ):
            raise StateConflict(
                "recovery fill coverage does not start at durable attempt"
            )
        if not isinstance(transport, TransportOutcomeEvidence):
            raise TypeError("transport must be TransportOutcomeEvidence")
        expected_outcome = (
            "unknown" if attempt.state == "unknown" else "response_received"
        )
        basis_allowed = transport.evidence_basis == "transport_result" or (
            transport.evidence_basis == "claim_expiry"
            and attempt.state == "unknown"
            and transport.outcome == "unknown"
        )
        if (
            attempt.transport_evidence_hash is None
            or transport.evidence_hash != attempt.transport_evidence_hash
            or transport.command_id != command.recovery_command_id
            or transport.attempt_id != attempt.attempt_id
            or transport.signed_evidence_hash != attempt.signed_evidence_hash
            or transport.outcome != expected_outcome
            or not basis_allowed
        ):
            raise StateConflict(
                "transport evidence differs from persisted recovery attempt"
            )
        if transport.attempted_at_ms > int(checked_at.timestamp() * 1_000):
            raise StateConflict("transport attempt is later than reconciliation cutoff")
        return checked_at, material

    def _close_proof(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        material: dict[str, object],
    ) -> tuple[RecoveryReconciliationProof, tuple[str, ...], str | None]:
        evidence_reasons: list[str] = []
        outcome_reasons: list[str] = []
        symbol = material.get("symbol")
        cloid = material.get("cloid")
        if not isinstance(symbol, str) or not isinstance(cloid, str):
            evidence_reasons.append("persisted_close_material_incomplete")
            symbol = "UNKNOWN"
            cloid = "0x" + "0" * 32
        status = _status_by_cloid(evidence).get(cloid)
        if set(_status_by_cloid(evidence)) != {cloid}:
            evidence_reasons.append("recovery_close_order_status_set_not_exact")
        signed = self.store.get_signed_recovery_evidence(
            command.recovery_command_id
        )
        if (
            signed.recovery_command_id != command.recovery_command_id
            or signed.recovery_hash != command.recovery_hash
            or signed.kind != command.kind
        ):
            raise StateConflict("signed recovery expiry differs from command")
        missing_safely_expired = (
            status is not None
            and status.state is VenueOrderState.MISSING
            and evidence.fill_coverage.requested_end_time_ms
            == snapshot.server_time_ms
            and snapshot.server_time_ms
            >= signed.expires_after_ms + _LATE_WRITE_SETTLEMENT_MS
            and evidence.fill_chain_complete
        )
        if not _definitive_status(status) or (
            status is not None
            and status.state is VenueOrderState.MISSING
            and not missing_safely_expired
        ):
            evidence_reasons.append("recovery_close_order_status_not_definitive")
        if (
            status is not None
            and status.state is VenueOrderState.MISSING
            and not missing_safely_expired
        ):
            evidence_reasons.append(
                "recovery_close_missing_before_signed_expiry_settled"
            )
        if (
            status is None
            or status.state is not VenueOrderState.ORDER
            or status.venue_status != "filled"
        ):
            outcome_reasons.append("recovery_close_not_confirmed_filled")
        if not evidence.fill_chain_complete:
            evidence_reasons.append("recovery_close_fill_chain_incomplete")
        fills = _fills_for(evidence.signed_fills, cloid)
        try:
            original = exact_decimal(
                material["original_signed_position"],
                field="original_signed_position",
            )
            close_size = exact_decimal(
                material["close_size"],
                field="close_size",
            )
        except (KeyError, ValidationError):
            original = _ZERO
            close_size = _ZERO
            evidence_reasons.append("persisted_close_economics_missing")
        if original == _ZERO or not _ZERO < close_size <= abs(original):
            evidence_reasons.append("persisted_close_economics_invalid")
        if status is not None and status.state is VenueOrderState.ORDER:
            if (
                status.symbol != symbol
                or status.reduce_only is not True
                or status.original_size != close_size
                or status.is_trigger is not False
            ):
                evidence_reasons.append(
                    "recovery_close_order_status_binding_mismatch"
                )
            if status.remaining_size != _ZERO:
                outcome_reasons.append("recovery_close_order_has_remaining_size")
        if any(item.cloid != cloid for item in evidence.signed_fills):
            evidence_reasons.append("recovery_close_fill_set_not_exact")
        expected_side = OrderSide.SELL if original > _ZERO else OrderSide.BUY
        filled_quantity = sum((item.quantity for item in fills), start=_ZERO)
        if filled_quantity != close_size:
            outcome_reasons.append("recovery_close_fill_quantity_mismatch")
        for fill in fills:
            if (
                fill.symbol != symbol
                or fill.side is not expected_side
                or fill.quantity != abs(fill.signed_quantity)
                or fill.end_position != fill.start_position + fill.signed_quantity
                or (
                    status is not None
                    and status.oid is not None
                    and fill.oid != status.oid
                )
            ):
                evidence_reasons.append("recovery_close_fill_binding_mismatch")
        signed_position = _position(snapshot, symbol)
        expected_remaining = max(abs(original) - close_size, _ZERO)
        flipped = (original > 0 and signed_position < 0) or (
            original < 0 and signed_position > 0
        )
        if flipped or abs(signed_position) != expected_remaining:
            outcome_reasons.append("recovery_close_not_exact_or_flipped")
        open_cloids = _open_cloids(snapshot)
        complete = not evidence_reasons
        success = complete and not outcome_reasons
        proof = RecoveryReconciliationProof(
            recovery_command_id=command.recovery_command_id,
            kind=command.kind,
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=evidence.observed_at,
            signed_position_quantity=signed_position,
            protected_quantity=_ZERO,
            open_order_cloids=open_cloids,
            affected_cloids=(cloid,),
            resolved_original_nonce=None,
            resolved_original_outcome=None,
            complete=complete,
            success=success,
        )
        resolution = "contained" if success and signed_position == _ZERO else None
        reasons = tuple(sorted(set(evidence_reasons + outcome_reasons)))
        return proof, reasons, resolution

    def _cancel_proof(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        material: dict[str, object],
    ) -> tuple[RecoveryReconciliationProof, tuple[str, ...], str | None]:
        evidence_reasons: list[str] = []
        outcome_reasons: list[str] = []
        requests = material.get("requests")
        if not isinstance(requests, list) or not requests:
            requested: tuple[str, ...] = ()
            evidence_reasons.append("persisted_cancel_requests_missing")
        else:
            parsed_requested = tuple(sorted(
                str(item.get("cloid"))
                for item in requests
                if isinstance(item, dict) and isinstance(item.get("cloid"), str)
            ))
            requested = tuple(sorted(set(parsed_requested)))
            if (
                len(parsed_requested) != len(requests)
                or len(requested) != len(parsed_requested)
            ):
                evidence_reasons.append("persisted_cancel_requests_invalid")
        statuses = _status_by_cloid(evidence)
        if set(statuses) != set(requested):
            evidence_reasons.append("cancel_order_status_set_not_exact")
        if any(not _definitive_status(statuses.get(cloid)) for cloid in requested):
            evidence_reasons.append("cancel_order_status_not_definitive")
        open_cloids = _open_cloids(snapshot)
        if set(requested) & set(open_cloids):
            outcome_reasons.append("canceled_cloid_still_open")
        parent_legs = self.store.get_legs(command.parent_command_id)
        stop_cloid = next(
            item.cloid for item in parent_legs if item.role == "protective_stop"
        )
        parent_plan = self.store.get_plan_payload(
            self.store.get_command(command.parent_command_id).plan_hash
        )
        instrument = parent_plan["entry"]["instrument"]
        symbol = instrument.removesuffix("-PERP")
        signed_position = _position(snapshot, symbol)
        coverage = snapshot.protection_coverage(
            symbol,
            expected_stop_cloids=(stop_cloid,),
        )
        if signed_position != _ZERO and stop_cloid in requested:
            outcome_reasons.append("cancel_would_remove_live_protective_stop")
        if signed_position != _ZERO and not coverage.fully_protected:
            outcome_reasons.append("post_cancel_position_not_fully_protected")
        complete = not evidence_reasons
        success = complete and not outcome_reasons
        proof = RecoveryReconciliationProof(
            recovery_command_id=command.recovery_command_id,
            kind=command.kind,
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=evidence.observed_at,
            signed_position_quantity=signed_position,
            protected_quantity=coverage.covered_size,
            open_order_cloids=open_cloids,
            affected_cloids=requested,
            resolved_original_nonce=None,
            resolved_original_outcome=None,
            complete=complete,
            success=success,
        )
        resolution = "contained" if success else None
        reasons = tuple(sorted(set(evidence_reasons + outcome_reasons)))
        return proof, reasons, resolution

    def _noop_proof(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        *,
        noncanonical_response_failure: bool = False,
    ) -> tuple[
        RecoveryReconciliationProof,
        tuple[str, ...],
        str | None,
    ]:
        evidence_reasons: list[str] = []
        outcome_reasons: list[str] = []
        parent_legs = self.store.get_legs(command.parent_command_id)
        parent_cloids = tuple(sorted(item.cloid for item in parent_legs))
        statuses = _status_by_cloid(evidence)
        if set(statuses) != set(parent_cloids):
            evidence_reasons.append("original_three_leg_status_set_not_exact")
        if any(
            not _definitive_status(statuses.get(cloid))
            for cloid in parent_cloids
        ):
            evidence_reasons.append("original_three_leg_outcome_not_definitive")
        if not evidence.fill_chain_complete:
            evidence_reasons.append("original_three_leg_fill_chain_incomplete")
        if evidence.signed_fills:
            outcome_reasons.append("fenced_original_action_has_unexpected_fills")
        original_attempt = self.store.get_attempt(command.parent_command_id)
        if (
            command.original_attempt_id != original_attempt.attempt_id
            or command.original_nonce != original_attempt.nonce
            or command.preflight_hash != original_attempt.preflight_hash
            or original_attempt.state != "unknown"
        ):
            raise StateConflict("noop command differs from original unknown attempt")
        parent_plan = self.store.get_plan_payload(
            self.store.get_command(command.parent_command_id).plan_hash
        )
        instrument = parent_plan["entry"]["instrument"]
        symbol = instrument.removesuffix("-PERP")
        signed_position = _position(snapshot, symbol)
        stop_cloid = next(
            item.cloid for item in parent_legs if item.role == "protective_stop"
        )
        coverage = snapshot.protection_coverage(
            symbol,
            expected_stop_cloids=(stop_cloid,),
        )
        if signed_position != _ZERO:
            outcome_reasons.append("fenced_original_action_left_unexpected_position")
        if any(status.state is not VenueOrderState.MISSING for status in statuses.values()):
            outcome_reasons.append("fenced_original_action_has_venue_order")
        if set(parent_cloids) & set(_open_cloids(snapshot)):
            outcome_reasons.append("fenced_original_action_cloid_still_open")
        account = snapshot.reconcile(
            owned_cloids=parent_cloids,
            allowed_position_symbols=(symbol,),
            expected_stop_cloids_by_symbol={symbol: (stop_cloid,)},
        )
        if (
            account.foreign_order_oids
            or account.foreign_position_symbols
            or account.orphan_protection_oids
            or account.halt_required
        ):
            outcome_reasons.append("fenced_original_action_account_state_unsafe")
        try:
            response = self.store.get_noop_fence_response(
                command.recovery_command_id
            )
        except RecordNotFound:
            response = None
            if noncanonical_response_failure:
                outcome_reasons.append("noop_response_not_canonical_default")
            else:
                evidence_reasons.append("noop_default_success_response_missing")
        if response is not None and (
            response.recovery_command_id != command.recovery_command_id
            or response.attempt_id
            != self.store.get_recovery_attempt(command.recovery_command_id).attempt_id
            or response.nonce != command.original_nonce
        ):
            raise StateConflict("noop response differs from exact recovery command")
        complete = not evidence_reasons
        success = complete and not outcome_reasons
        venue_activity = bool(
            evidence.signed_fills
            or signed_position != _ZERO
            or any(
                status.state is not VenueOrderState.MISSING
                for status in statuses.values()
            )
        )
        resolved_outcome = (
            None
            if not complete
            else "accepted"
            if venue_activity
            else "rejected"
            if noncanonical_response_failure
            else "fenced"
        )
        proof = RecoveryReconciliationProof(
            recovery_command_id=command.recovery_command_id,
            kind=command.kind,
            account_snapshot_hash=snapshot.snapshot_hash,
            observed_at=evidence.observed_at,
            signed_position_quantity=signed_position,
            protected_quantity=coverage.covered_size,
            open_order_cloids=_open_cloids(snapshot),
            affected_cloids=parent_cloids,
            resolved_original_nonce=(command.original_nonce if complete else None),
            resolved_original_outcome=resolved_outcome,
            complete=complete,
            success=success,
        )
        reasons = tuple(sorted(set(evidence_reasons + outcome_reasons)))
        return (
            proof,
            reasons,
            (
                "contained"
                if success
                and (
                    signed_position == _ZERO
                    or coverage.covered_size == abs(signed_position)
                )
                else None
            ),
        )

    def reconcile(
        self,
        recovery_command_id: str,
        worker_id: str,
        *,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        transport: TransportOutcomeEvidence | None = None,
        at: datetime,
    ) -> RecoveryCoordinationResult:
        checked_at = _utc(at, "at")
        command = self.store.get_recovery_command(recovery_command_id)
        persisted_transport = self.store.get_recovery_transport_evidence(
            recovery_command_id
        )
        if transport is None:
            transport = persisted_transport
        elif not isinstance(transport, TransportOutcomeEvidence) or (
            transport.evidence_hash != persisted_transport.evidence_hash
        ):
            raise StateConflict(
                "caller transport differs from durable recovery evidence"
            )
        _, material = self._validate_common(
            command, transport, snapshot, evidence, checked_at
        )
        attempt = self.store.get_recovery_attempt(recovery_command_id)
        noncanonical_noop_failure = (
            attempt.state == "unknown"
            and command.kind == "noop_fence"
            and transport.detail_code
            == "noop_response_not_canonical_default"
        )
        if (
            attempt.state == "unknown"
            and command.kind == "noop_fence"
            and not noncanonical_noop_failure
        ):
            proof = RecoveryReconciliationProof(
                recovery_command_id=command.recovery_command_id,
                kind=command.kind,
                account_snapshot_hash=snapshot.snapshot_hash,
                observed_at=checked_at,
                signed_position_quantity=_position(
                    snapshot,
                    str(material.get("symbol", "UNKNOWN")),
                ),
                protected_quantity=_ZERO,
                open_order_cloids=_open_cloids(snapshot),
                affected_cloids=(),
                resolved_original_nonce=None,
                resolved_original_outcome=None,
                complete=False,
                success=False,
            )
            reasons = ("noop_transport_outcome_unknown",)
            resolution = None
            schema_change = None
        elif command.kind == "reduce_only_close":
            proof, reasons, resolution = self._close_proof(
                command, snapshot, evidence, material
            )
            schema_change = None
        elif command.kind == "cancel_by_cloid":
            proof, reasons, resolution = self._cancel_proof(
                command, snapshot, evidence, material
            )
            schema_change = None
        else:
            proof, reasons, resolution = self._noop_proof(
                command,
                snapshot,
                evidence,
                noncanonical_response_failure=noncanonical_noop_failure,
            )
            schema_change = None
        # Acquire the fenced mutation lease only after all read-only evidence
        # validation and proof derivation have succeeded.  Malformed caller
        # input therefore cannot hold the recovery lane until lease expiry.
        mutation_at = self._mutation_time(checked_at)
        mutation_ms = int(mutation_at.timestamp() * 1_000)
        if (
            mutation_at < evidence.observed_at
            or mutation_ms - snapshot.server_time_ms > 5_000
        ):
            raise StateConflict(
                "recovery evidence became stale before mutation claim"
            )
        reconciliation_id = domain_hash(
            "trading-harness/recovery-reconciliation-coordinator/v1",
            {
                "recovery_command_id": command.recovery_command_id,
                "proof_hash": proof.proof_hash,
                "venue_evidence_hash": evidence.evidence_hash,
                "transport_evidence_hash": transport.evidence_hash,
            },
        )
        recovery_fills = (
            tuple(
                RecoveryVenueFill(
                    fill_id=fill.fill_id,
                    recovery_command_id=command.recovery_command_id,
                    parent_command_id=command.parent_command_id,
                    cloid=fill.cloid,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    quantity=fill.quantity,
                    signed_quantity=fill.signed_quantity,
                    start_position=fill.start_position,
                    end_position=fill.end_position,
                    price=fill.price,
                    fee=fill.fee,
                    closed_pnl=fill.closed_pnl,
                    fee_token=fill.fee_token,
                    crossed=fill.crossed,
                    builder_fee=fill.builder_fee,
                    venue_oid=fill.oid,
                    venue_trade_id=fill.tid,
                    transaction_hash=fill.transaction_hash,
                    occurred_at=_EPOCH + timedelta(milliseconds=fill.time_ms),
                    observed_at=checked_at,
                    account_snapshot_hash=snapshot.snapshot_hash,
                    venue_evidence_hash=evidence.evidence_hash,
                )
                for fill in evidence.signed_fills
            )
            if command.kind == "reduce_only_close"
            else ()
        )
        claim = self.store.claim_recovery_reconciliation(
            recovery_command_id,
            worker_id,
            at=mutation_at,
            lease_seconds=self.lease_seconds,
        )
        apply_at = self._mutation_time(mutation_at)
        apply_ms = int(apply_at.timestamp() * 1_000)
        if (
            apply_at < evidence.observed_at
            or apply_ms - snapshot.server_time_ms > 5_000
            or claim.lease_expires_at is None
            or apply_at >= claim.lease_expires_at
        ):
            self.store.release_recovery_reconciliation_claim(
                command.recovery_command_id,
                worker_id,
                claim.fencing_token,
                at=apply_at,
                reason="evidence_stale_after_claim",
            )
            return RecoveryCoordinationResult(
                recovery_command_id=command.recovery_command_id,
                recovery_state=self.store.get_recovery_command(
                    command.recovery_command_id
                ).state,
                proof=proof,
                incomplete_reasons=tuple(
                    sorted(set((*reasons, "evidence_stale_after_claim")))
                ),
                incident_resolution=None,
                required_schema_change=schema_change,
            )
        try:
            state = self.store.reconcile_recovery(
                command.recovery_command_id,
                worker_id,
                claim.fencing_token,
                reconciliation_id=reconciliation_id,
                proof=proof,
                incident_resolution=resolution,
                fills=recovery_fills,
                mutation_at=apply_at,
            )
        except Exception as error:
            current = self.store.get_recovery_outbox(
                command.recovery_command_id
            )
            if (
                current.state == "reconciling"
                and current.worker_id == worker_id
                and current.fencing_token == claim.fencing_token
            ):
                self.store.release_recovery_reconciliation_claim(
                    command.recovery_command_id,
                    worker_id,
                    claim.fencing_token,
                    at=self._mutation_time(apply_at),
                    reason="apply_failed:" + type(error).__name__,
                )
            raise
        return RecoveryCoordinationResult(
            recovery_command_id=command.recovery_command_id,
            recovery_state=state.state,
            proof=proof,
            incomplete_reasons=reasons,
            incident_resolution=resolution,
            required_schema_change=schema_change,
        )


__all__ = (
    "RECOVERY_VENUE_READ_HASH_DOMAIN",
    "RecoveryCoordinationResult",
    "RecoveryReconciliationCoordinator",
    "RecoveryVenueRead",
)
