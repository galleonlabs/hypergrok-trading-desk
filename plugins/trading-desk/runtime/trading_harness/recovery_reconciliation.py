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
from typing import Iterable

from .canonical import domain_hash
from .errors import RecordNotFound, StateConflict, ValidationError
from .execution_store import (
    ExecutionStore,
    RecoveryCommand,
    RecoveryReconciliationProof,
    TransportOutcomeEvidence,
)
from .hyperliquid_account import HyperliquidAccountSnapshot, OrderSide
from .hyperliquid_reconcile import (
    FillCoverage,
    ParsedOrderStatus,
    SignedFillEvidence,
    VenueOrderState,
)
from .market_data import public_info_endpoint
from .policy import exact_decimal
from .reconciliation_coordinator import _verify_snapshot_hash


RECOVERY_VENUE_READ_HASH_DOMAIN = (
    "trading-harness/hyperliquid-recovery-venue-read/v1"
)
_MAX_SNAPSHOT_AGE = timedelta(seconds=5)
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
        if any(not isinstance(item, ParsedOrderStatus) for item in statuses):
            raise TypeError("order_statuses must contain ParsedOrderStatus")
        if any(not isinstance(item, SignedFillEvidence) for item in fills):
            raise TypeError("signed_fills must contain SignedFillEvidence")
        if not isinstance(self.fill_coverage, FillCoverage):
            raise TypeError("fill_coverage must be FillCoverage")
        requested = tuple(item.requested_cloid for item in statuses)
        if len(requested) != len(set(requested)):
            raise ValidationError("recovery venue read repeats a CLOID status")
        fill_ids = tuple(item.fill_id for item in fills)
        if len(fill_ids) != len(set(fill_ids)):
            raise ValidationError("recovery venue read repeats fill identity")
        if self.fill_coverage.unique_fills != len(fills):
            raise ValidationError("fill coverage count differs from signed fills")
        observed_ms = int(self.observed_at.timestamp() * 1_000)
        if any(
            item.status_timestamp_ms is not None
            and item.status_timestamp_ms > observed_ms
            for item in statuses
        ):
            raise ValidationError("order status is later than venue read cutoff")
        if any(item.time_ms > observed_ms for item in fills):
            raise ValidationError("fill is later than venue read cutoff")
        object.__setattr__(self, "order_statuses", statuses)
        object.__setattr__(self, "signed_fills", fills)
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

    def __init__(self, store: ExecutionStore, *, lease_seconds: int = 15) -> None:
        if not isinstance(store, ExecutionStore):
            raise TypeError("store must be ExecutionStore")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 60:
            raise ValidationError("lease_seconds must be from 5 to 60")
        if store.environment.value != "testnet":
            raise ValidationError("recovery coordinator is testnet-only")
        self.store = store
        self.lease_seconds = lease_seconds

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
            fill.time_ms > snapshot.server_time_ms for fill in evidence.signed_fills
        ):
            raise StateConflict("recovery venue facts postdate account snapshot")
        if evidence.fill_coverage.requested_end_time_ms > snapshot.server_time_ms:
            raise StateConflict("fill coverage extends beyond account snapshot")
        attempt = self.store.get_recovery_attempt(command.recovery_command_id)
        if not isinstance(transport, TransportOutcomeEvidence):
            raise TypeError("transport must be TransportOutcomeEvidence")
        expected_outcome = (
            "unknown" if attempt.state == "unknown" else "response_received"
        )
        if (
            attempt.transport_evidence_hash is None
            or transport.evidence_hash != attempt.transport_evidence_hash
            or transport.command_id != command.recovery_command_id
            or transport.attempt_id != attempt.attempt_id
            or transport.signed_evidence_hash != attempt.signed_evidence_hash
            or transport.outcome != expected_outcome
            or transport.evidence_basis != "transport_result"
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
        reasons: list[str] = []
        symbol = material.get("symbol")
        cloid = material.get("cloid")
        if not isinstance(symbol, str) or not isinstance(cloid, str):
            reasons.append("persisted_close_material_incomplete")
            symbol = "UNKNOWN"
            cloid = "0x" + "0" * 32
        status = _status_by_cloid(evidence).get(cloid)
        if set(_status_by_cloid(evidence)) != {cloid}:
            reasons.append("recovery_close_order_status_set_not_exact")
        if not _definitive_status(status):
            reasons.append("recovery_close_order_status_not_definitive")
        if (
            status is None
            or status.state is not VenueOrderState.ORDER
            or status.venue_status != "filled"
        ):
            reasons.append("recovery_close_not_confirmed_filled")
        if not evidence.fill_chain_complete:
            reasons.append("recovery_close_fill_chain_incomplete")
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
            reasons.append("persisted_close_economics_missing")
        if original == _ZERO or not _ZERO < close_size <= abs(original):
            reasons.append("persisted_close_economics_invalid")
        if status is not None and status.state is VenueOrderState.ORDER:
            if (
                status.symbol != symbol
                or status.reduce_only is not True
                or status.original_size != close_size
                or status.remaining_size != _ZERO
                or status.is_trigger is not False
            ):
                reasons.append("recovery_close_order_status_binding_mismatch")
        if any(item.cloid != cloid for item in evidence.signed_fills):
            reasons.append("recovery_close_fill_set_not_exact")
        expected_side = OrderSide.SELL if original > _ZERO else OrderSide.BUY
        filled_quantity = sum((item.quantity for item in fills), start=_ZERO)
        if filled_quantity != close_size:
            reasons.append("recovery_close_fill_quantity_mismatch")
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
                reasons.append("recovery_close_fill_binding_mismatch")
        for left, right in zip(fills, fills[1:]):
            if left.end_position != right.start_position:
                reasons.append("recovery_close_fill_chain_discontinuous")
        if fills and fills[0].start_position != original:
            reasons.append("recovery_close_fill_start_mismatch")
        signed_position = _position(snapshot, symbol)
        if fills and fills[-1].end_position != signed_position:
            reasons.append("recovery_close_fill_end_mismatch")
        expected_remaining = max(abs(original) - close_size, _ZERO)
        flipped = (original > 0 and signed_position < 0) or (
            original < 0 and signed_position > 0
        )
        if flipped or abs(signed_position) != expected_remaining:
            reasons.append("recovery_close_not_exact_or_flipped")
        open_cloids = _open_cloids(snapshot)
        complete = not reasons
        success = complete
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
        return proof, tuple(sorted(set(reasons))), resolution

    def _cancel_proof(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
        material: dict[str, object],
    ) -> tuple[RecoveryReconciliationProof, tuple[str, ...], str | None]:
        reasons: list[str] = []
        requests = material.get("requests")
        if not isinstance(requests, list) or not requests:
            requested: tuple[str, ...] = ()
            reasons.append("persisted_cancel_requests_missing")
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
                reasons.append("persisted_cancel_requests_invalid")
        statuses = _status_by_cloid(evidence)
        if set(statuses) != set(requested):
            reasons.append("cancel_order_status_set_not_exact")
        if any(not _definitive_status(statuses.get(cloid)) for cloid in requested):
            reasons.append("cancel_order_status_not_definitive")
        open_cloids = _open_cloids(snapshot)
        if set(requested) & set(open_cloids):
            reasons.append("canceled_cloid_still_open")
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
            reasons.append("cancel_would_remove_live_protective_stop")
        if signed_position != _ZERO and not coverage.fully_protected:
            reasons.append("post_cancel_position_not_fully_protected")
        complete = not reasons
        success = complete
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
        return proof, tuple(sorted(set(reasons))), resolution

    def _noop_proof(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        evidence: RecoveryVenueRead,
    ) -> tuple[
        RecoveryReconciliationProof,
        tuple[str, ...],
        str | None,
    ]:
        reasons: list[str] = []
        parent_legs = self.store.get_legs(command.parent_command_id)
        parent_cloids = tuple(sorted(item.cloid for item in parent_legs))
        statuses = _status_by_cloid(evidence)
        if set(statuses) != set(parent_cloids):
            reasons.append("original_three_leg_status_set_not_exact")
        if any(
            not _definitive_status(statuses.get(cloid))
            for cloid in parent_cloids
        ):
            reasons.append("original_three_leg_outcome_not_definitive")
        if not evidence.fill_chain_complete:
            reasons.append("original_three_leg_fill_chain_incomplete")
        if evidence.signed_fills:
            reasons.append("fenced_original_action_has_unexpected_fills")
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
            reasons.append("fenced_original_action_left_unexpected_position")
        if any(status.state is not VenueOrderState.MISSING for status in statuses.values()):
            reasons.append("fenced_original_action_has_venue_order")
        if set(parent_cloids) & set(_open_cloids(snapshot)):
            reasons.append("fenced_original_action_cloid_still_open")
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
            reasons.append("fenced_original_action_account_state_unsafe")
        try:
            response = self.store.get_noop_fence_response(
                command.recovery_command_id
            )
        except RecordNotFound:
            response = None
            reasons.append("noop_default_success_response_missing")
        if response is not None and (
            response.recovery_command_id != command.recovery_command_id
            or response.attempt_id
            != self.store.get_recovery_attempt(command.recovery_command_id).attempt_id
            or response.nonce != command.original_nonce
        ):
            raise StateConflict("noop response differs from exact recovery command")
        complete = not reasons
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
            resolved_original_outcome=("fenced" if complete else None),
            complete=complete,
            success=complete,
        )
        return (
            proof,
            tuple(sorted(set(reasons))),
            "contained" if complete else None,
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
        if attempt.state == "unknown":
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
            reasons = ("recovery_transport_outcome_unknown",)
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
                command, snapshot, evidence
            )
            schema_change = None
        # Acquire the fenced mutation lease only after all read-only evidence
        # validation and proof derivation have succeeded.  Malformed caller
        # input therefore cannot hold the recovery lane until lease expiry.
        claim = self.store.claim_recovery_reconciliation(
            recovery_command_id,
            worker_id,
            at=checked_at,
            lease_seconds=self.lease_seconds,
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
        state = self.store.reconcile_recovery(
            command.recovery_command_id,
            worker_id,
            claim.fencing_token,
            reconciliation_id=reconciliation_id,
            proof=proof,
            incident_resolution=resolution,
        )
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
