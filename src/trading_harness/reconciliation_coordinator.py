"""Fail-closed coordinator for main protected-entry reconciliation.

``ExecutionStore.reconcile`` is a low-level persistence transition retained for
compatibility.  Capital-path callers should use this module instead: it accepts
only a concrete :class:`VenueReconciliationBundle` (or obtains one from the
typed :class:`HyperliquidVenueReconciler`), independently verifies its source
snapshot, hash, owned-leg bindings, fills, protection and completeness, and
only then forwards the exact bundle fields to the store.

No arbitrary account hash, position quantity or ``complete`` boolean is
accepted by the public coordinator API.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from typing import Any

from .canonical import canonical_decimal, domain_hash
from .errors import RecordNotFound, StateConflict, ValidationError
from .execution_store import (
    ExecutionStore,
    LegRecord,
    NoopFenceResolution,
    VenueFill,
)
from .hyperliquid_account import (
    ACCOUNT_SNAPSHOT_HASH_DOMAIN,
    METADATA_SNAPSHOT_HASH_DOMAIN,
    HyperliquidAccountSnapshot,
    OrderSide,
)
from . import hyperliquid_reconcile as _venue_module
from .hyperliquid_reconcile import (
    InfoTransport,
    OwnedLeg,
    SignedFillEvidence,
    VenueOrderState,
    VenueReconciliationBundle,
    reconcile_hyperliquid_venue,
    canonical_hyperliquid_fill_id,
)
from .hyperliquid_wire import HyperliquidNetwork
from .market_data import post_public_info
from .market_data import public_info_endpoint
from .policy import decimal_subtract


_ROLES = ("entry", "protective_stop", "take_profit")
_ZERO = Decimal("0")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_AGE_MS = 5_000
_MAX_FUTURE_SKEW_MS = 5_000
_EXACT_CONTEXT = Context(prec=256)


Clock = Callable[[], datetime]


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clock_ms(clock: Clock) -> int:
    if not callable(clock):
        raise TypeError("clock must be callable")
    try:
        value = _utc(clock(), "reconciliation clock")
    except Exception as error:
        if isinstance(error, (TypeError, ValidationError)):
            raise
        raise ValidationError(
            f"reconciliation clock failed: {type(error).__name__}"
        ) from error
    delta = value - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError("reconciliation clock predates Unix epoch")
    return result


def _datetime_ms(value: datetime) -> int:
    delta = _utc(value, "observed_at") - _EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _sum_decimal(values: list[Decimal]) -> Decimal:
    with localcontext(_EXACT_CONTEXT) as context:
        total = _ZERO
        for value in values:
            total = context.add(total, value)
        return total


def _bundle_material(bundle: VenueReconciliationBundle) -> dict[str, object]:
    return {
        "network": bundle.network.value,
        "main_account_address": bundle.main_account_address,
        "account_id": bundle.account_id,
        "command_id": bundle.command_id,
        "plan_hash": bundle.plan_hash,
        "account_snapshot_hash": bundle.account_snapshot_hash,
        "observed_at": bundle.observed_at,
        "order_statuses": [item.canonical_record() for item in bundle.order_statuses],
        "signed_fills": [item.canonical_record() for item in bundle.signed_fills],
        "auxiliary_order_statuses": [
            item.canonical_record() for item in bundle.auxiliary_order_statuses
        ],
        "auxiliary_fills": [
            item.canonical_record() for item in bundle.auxiliary_fills
        ],
        "fill_coverage": bundle.fill_coverage.as_dict(),
        "legs": [item.as_dict() for item in bundle.legs],
        "signed_position_quantity": canonical_decimal(
            bundle.signed_position_quantity
        ),
        "protected_quantity": canonical_decimal(bundle.protected_quantity),
        "complete": bundle.complete,
        "incomplete_reasons": list(bundle.incomplete_reasons),
    }


def _verify_snapshot_hash(snapshot: HyperliquidAccountSnapshot) -> None:
    records = [
        {
            "symbol": item.symbol,
            "asset_id": item.asset_id,
            "sz_decimals": item.sz_decimals,
            "max_leverage": canonical_decimal(item.max_leverage),
            "margin_mode": item.margin_mode,
            "margin_table_id": item.margin_table_id,
            "is_delisted": item.is_delisted,
        }
        for item in snapshot.metadata.instruments
    ]
    metadata_hash = domain_hash(
        METADATA_SNAPSHOT_HASH_DOMAIN,
        {
            "collateral_token": snapshot.metadata.collateral_token,
            "instruments": records,
        },
    )
    if metadata_hash != snapshot.metadata.metadata_hash:
        raise StateConflict("account metadata hash does not match contents")
    for item, record in zip(snapshot.metadata.instruments, records):
        expected = domain_hash(
            "trading-harness/hyperliquid-perp-instrument/v1",
            {**record, "metadata_snapshot_hash": metadata_hash},
        )
        if item.metadata_hash != expected:
            raise StateConflict("account instrument hash does not match metadata")
    material = {
        "schema_version": "hyperliquid.account_snapshot.v1",
        "venue": "hyperliquid",
        "network": snapshot.network,
        "main_account_address": snapshot.main_account_address,
        "account_mode": snapshot.account_mode.value,
        "server_time_ms": snapshot.server_time_ms,
        "margin_summary": snapshot.margin_summary.canonical_record(),
        "cross_margin_summary": snapshot.cross_margin_summary.canonical_record(),
        "cross_maintenance_margin_used": canonical_decimal(
            snapshot.cross_maintenance_margin_used
        ),
        "withdrawable": canonical_decimal(snapshot.withdrawable),
        "positions": [item.canonical_record() for item in snapshot.positions],
        "open_orders": [item.canonical_record() for item in snapshot.open_orders],
        "metadata_hash": metadata_hash,
    }
    if domain_hash(ACCOUNT_SNAPSHOT_HASH_DOMAIN, material) != snapshot.snapshot_hash:
        raise StateConflict("account snapshot hash does not match contents")


def _symbol_for_instrument(
    snapshot: HyperliquidAccountSnapshot, instrument: str
) -> str:
    matches = tuple(
        item.symbol
        for item in snapshot.metadata.instruments
        if instrument in {item.symbol, f"{item.symbol}-PERP"}
    )
    if len(matches) != 1:
        raise StateConflict("plan instrument is not unique in account metadata")
    return matches[0]


def _owned_legs(
    store: ExecutionStore,
    command_id: str,
    snapshot: HyperliquidAccountSnapshot,
) -> tuple[OwnedLeg, ...]:
    command = store.get_command(command_id)
    plan = store.get_plan_payload(command.plan_hash)
    instrument = plan.get("entry", {}).get("instrument") if isinstance(plan.get("entry"), Mapping) else None
    if not isinstance(instrument, str):
        raise StateConflict("persisted protected plan lacks an entry instrument")
    symbol = _symbol_for_instrument(snapshot, instrument)
    records = {record.role: record for record in store.get_legs(command_id)}
    if set(records) != set(_ROLES):
        raise StateConflict("command does not contain exactly three durable legs")
    return tuple(
        OwnedLeg(
            role=role,
            cloid=records[role].cloid,
            symbol=symbol,
            side=OrderSide(records[role].side),
            requested_quantity=records[role].requested_quantity,
        )
        for role in _ROLES
    )


@dataclass(frozen=True, slots=True)
class HyperliquidVenueReconciler:
    """Typed read-only adapter around the reviewed Hyperliquid reconciler."""

    transport: InfoTransport = post_public_info
    clock: Clock = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if not callable(self.transport) or not callable(self.clock):
            raise TypeError("transport and clock must be callable")

    def reconcile(
        self,
        snapshot: HyperliquidAccountSnapshot,
        owned_legs: tuple[OwnedLeg, ...],
        *,
        account_id: str,
        command_id: str,
        plan_hash: str,
        network: HyperliquidNetwork,
        fills_start_time_ms: int,
        fills_end_time_ms: int | None = None,
        store: ExecutionStore | None = None,
    ) -> VenueReconciliationBundle:
        bundle = reconcile_hyperliquid_venue(
            snapshot,
            owned_legs,
            account_id=account_id,
            command_id=command_id,
            plan_hash=plan_hash,
            network=network,
            fills_start_time_ms=fills_start_time_ms,
            fills_end_time_ms=fills_end_time_ms,
            transport=self.transport,
            clock=self.clock,
            store=store,
        )
        if not isinstance(bundle, VenueReconciliationBundle):
            raise TypeError("venue reconciler did not return VenueReconciliationBundle")
        return bundle


@dataclass(frozen=True, slots=True)
class MainReconciliationResult:
    command_id: str
    reconciliation_hash: str
    command_state: str
    evidence_complete: bool
    terminal: bool
    protection_state: str
    signed_position_quantity: Decimal
    protected_quantity: Decimal
    risk_released_loss: Decimal
    risk_released_notional: Decimal
    residual_command_reserved_loss: Decimal
    residual_command_reserved_notional: Decimal
    account_reserved_loss: Decimal
    account_reserved_notional: Decimal
    active_incident_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "main_reconciliation_result.v1",
            "command_id": self.command_id,
            "reconciliation_hash": self.reconciliation_hash,
            "command_state": self.command_state,
            "evidence_complete": self.evidence_complete,
            "terminal": self.terminal,
            "protection_state": self.protection_state,
            "signed_position_quantity": canonical_decimal(
                self.signed_position_quantity
            ),
            "protected_quantity": canonical_decimal(self.protected_quantity),
            "risk_released_loss": canonical_decimal(self.risk_released_loss),
            "risk_released_notional": canonical_decimal(
                self.risk_released_notional
            ),
            "residual_command_reserved_loss": canonical_decimal(
                self.residual_command_reserved_loss
            ),
            "residual_command_reserved_notional": canonical_decimal(
                self.residual_command_reserved_notional
            ),
            "account_reserved_loss": canonical_decimal(self.account_reserved_loss),
            "account_reserved_notional": canonical_decimal(
                self.account_reserved_notional
            ),
            "active_incident_ids": list(self.active_incident_ids),
        }


class MainEntryReconciliationCoordinator:
    """Validate typed venue truth before invoking the legacy store transition."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        network: HyperliquidNetwork,
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(store, ExecutionStore):
            raise TypeError("store must be ExecutionStore")
        if not isinstance(network, HyperliquidNetwork):
            raise TypeError("network must be HyperliquidNetwork")
        if store.environment.value != network.value:
            raise ValidationError("coordinator network differs from execution store")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.store = store
        self.network = network
        self.clock = clock

    def reconcile_and_apply(
        self,
        reconciler: HyperliquidVenueReconciler,
        snapshot: HyperliquidAccountSnapshot,
        *,
        command_id: str,
        worker_id: str,
        fencing_token: int,
        reconciliation_id: str,
        fills_start_time_ms: int,
        fills_end_time_ms: int | None = None,
    ) -> MainReconciliationResult:
        if not isinstance(reconciler, HyperliquidVenueReconciler):
            raise TypeError("reconciler must be HyperliquidVenueReconciler")
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        bundle = self.read_bundle(
            reconciler,
            snapshot,
            command_id=command_id,
            fills_start_time_ms=fills_start_time_ms,
            fills_end_time_ms=fills_end_time_ms,
        )
        return self.apply_bundle(
            bundle,
            snapshot,
            worker_id=worker_id,
            fencing_token=fencing_token,
            reconciliation_id=reconciliation_id,
        )

    def read_bundle(
        self,
        reconciler: HyperliquidVenueReconciler,
        snapshot: HyperliquidAccountSnapshot,
        *,
        command_id: str,
        fills_start_time_ms: int,
        fills_end_time_ms: int | None = None,
    ) -> VenueReconciliationBundle:
        """Perform only allowlisted venue reads before acquiring a mutation lease."""

        if not isinstance(reconciler, HyperliquidVenueReconciler):
            raise TypeError("reconciler must be HyperliquidVenueReconciler")
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        command = self.store.get_command(command_id)
        owned = _owned_legs(self.store, command_id, snapshot)
        return reconciler.reconcile(
            snapshot,
            owned,
            account_id=self.store.account_id,
            command_id=command.command_id,
            plan_hash=command.plan_hash,
            network=self.network,
            fills_start_time_ms=fills_start_time_ms,
            fills_end_time_ms=fills_end_time_ms,
            store=self.store,
        )

    def apply_bundle(
        self,
        bundle: VenueReconciliationBundle,
        snapshot: HyperliquidAccountSnapshot,
        *,
        worker_id: str,
        fencing_token: int,
        reconciliation_id: str,
    ) -> MainReconciliationResult:
        if not isinstance(bundle, VenueReconciliationBundle):
            raise TypeError("bundle must be VenueReconciliationBundle")
        if not isinstance(snapshot, HyperliquidAccountSnapshot):
            raise TypeError("snapshot must be HyperliquidAccountSnapshot")
        now_ms = _clock_ms(self.clock)
        effective_complete, fence = self._verify_bundle(
            bundle,
            snapshot,
            now_ms=now_ms,
        )
        command_before = self.store.get_command(bundle.command_id)
        reserved_before = self.store.get_reserved_exposure()
        store_arguments = bundle.execution_store_kwargs()
        store_arguments["complete"] = effective_complete
        store_arguments["mutation_at"] = _EPOCH + timedelta(
            milliseconds=now_ms
        )
        applied_reconciliation_id = (
            reconciliation_id
            if fence is None
            else domain_hash(
                "trading-harness/fenced-parent-reconciliation-id/v1",
                {
                    "requested_reconciliation_id": reconciliation_id,
                    "venue_reconciliation_hash": bundle.reconciliation_hash,
                    "noop_fence_resolution_hash": fence.resolution_hash,
                },
            )
        )
        command_after = self.store.reconcile(
            bundle.command_id,
            worker_id,
            fencing_token,
            reconciliation_id=applied_reconciliation_id,
            **store_arguments,
        )
        if (
            command_after.state == "reconciling"
            and self.store.get_attempt(bundle.command_id).state == "unknown"
            and bundle.fill_coverage.complete
            and not bundle.signed_fills
            and len(bundle.order_statuses) == 3
            and all(
                status.state is VenueOrderState.MISSING
                for status in bundle.order_statuses
            )
            and bundle.signed_position_quantity == _ZERO
            and not set(item.requested_cloid for item in bundle.order_statuses)
            & set(order.cloid for order in snapshot.all_open_orders() if order.cloid)
        ):
            existing_ambiguity = tuple(
                item
                for item in self.store.list_incidents(bundle.command_id)
                if item.code == "UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING"
                and item.state != "closed"
            )
            if not existing_ambiguity:
                attempt = self.store.get_attempt(bundle.command_id)
                self.store.record_incident(
                    incident_id=domain_hash(
                        "trading-harness/unknown-submission-ambiguity/v1",
                        {
                            "command_id": bundle.command_id,
                            "attempt_id": attempt.attempt_id,
                            "nonce": attempt.nonce,
                            "action_hash": attempt.action_hash,
                            "wire_hash": attempt.wire_hash,
                        },
                    ),
                    command_id=bundle.command_id,
                    code="UNKNOWN_SUBMISSION_ALL_CLOIDS_MISSING",
                    severity="critical",
                    at=bundle.observed_at,
                    details={
                        "attempt_id": attempt.attempt_id,
                        "account_snapshot_hash": bundle.account_snapshot_hash,
                        "venue_reconciliation_hash": bundle.reconciliation_hash,
                        "requires_same_nonce_fence": True,
                    },
                )
        reserved_after = self.store.get_reserved_exposure()
        protection = self.store.get_protection(bundle.command_id)
        terminal = command_after.state == "terminal"
        if terminal and fence is not None:
            self.store.close_noop_fenced_incident(
                bundle.command_id,
                fence.resolution_hash,
                at=bundle.observed_at,
            )
        incidents = tuple(
            item.incident_id
            for item in self.store.list_incidents(bundle.command_id)
            if item.state != "closed"
        )
        return MainReconciliationResult(
            command_id=bundle.command_id,
            reconciliation_hash=domain_hash(
                "trading-harness/main-reconciliation-evidence/v2",
                {
                    "venue_reconciliation_hash": bundle.reconciliation_hash,
                    "effective_complete": effective_complete,
                    "noop_fence_resolution_hash": (
                        None if fence is None else fence.resolution_hash
                    ),
                },
            ),
            command_state=command_after.state,
            evidence_complete=effective_complete,
            terminal=terminal,
            protection_state=protection.state,
            signed_position_quantity=bundle.signed_position_quantity,
            protected_quantity=bundle.protected_quantity,
            risk_released_loss=decimal_subtract(
                reserved_before[0], reserved_after[0], field="released reserved loss"
            ),
            risk_released_notional=decimal_subtract(
                reserved_before[1],
                reserved_after[1],
                field="released reserved notional",
            ),
            residual_command_reserved_loss=(
                _ZERO if terminal else command_before.reserved_loss
            ),
            residual_command_reserved_notional=(
                _ZERO if terminal else command_before.reserved_notional
            ),
            account_reserved_loss=reserved_after[0],
            account_reserved_notional=reserved_after[1],
            active_incident_ids=incidents,
        )

    def _verify_bundle(
        self,
        bundle: VenueReconciliationBundle,
        snapshot: HyperliquidAccountSnapshot,
        *,
        now_ms: int,
    ) -> tuple[bool, NoopFenceResolution | None]:
        if domain_hash(
            _venue_module.VENUE_RECONCILIATION_HASH_DOMAIN,
            _bundle_material(bundle),
        ) != bundle.reconciliation_hash:
            raise StateConflict("venue reconciliation hash does not match contents")
        _verify_snapshot_hash(snapshot)
        if (
            bundle.network is not self.network
            or snapshot.network != self.network.value
            or bundle.account_id != self.store.account_id
            or bundle.main_account_address != snapshot.main_account_address
            or bundle.account_snapshot_hash != snapshot.snapshot_hash
        ):
            raise StateConflict("venue reconciliation scope differs from store/snapshot")
        observed_ms = _datetime_ms(bundle.observed_at)
        if observed_ms != snapshot.server_time_ms:
            raise StateConflict("bundle observation time differs from account snapshot")
        age = now_ms - snapshot.server_time_ms
        if age > _MAX_AGE_MS or age < -_MAX_FUTURE_SKEW_MS:
            raise StateConflict("account evidence is stale for reconciliation")
        if snapshot.received_at_ms < snapshot.server_time_ms or snapshot.received_at_ms > now_ms:
            raise StateConflict("account receipt time is invalid for reconciliation")
        if snapshot.age_ms != snapshot.received_at_ms - snapshot.server_time_ms:
            raise StateConflict("account snapshot age does not match its timestamps")
        if snapshot.source_url != public_info_endpoint(self.network.value):
            raise StateConflict("account snapshot source URL is not allowlisted")

        command = self.store.get_command(bundle.command_id)
        if command.plan_hash != bundle.plan_hash:
            raise StateConflict("bundle plan differs from durable command")
        owned = _owned_legs(self.store, bundle.command_id, snapshot)
        owned_by_role = {item.role: item for item in owned}
        statuses = {item.role: item for item in bundle.order_statuses}
        updates = {item.role: item for item in bundle.legs}
        if (
            len(bundle.order_statuses) != 3
            or len(bundle.legs) != 3
            or set(statuses) != set(_ROLES)
            or set(updates) != set(_ROLES)
        ):
            raise StateConflict("bundle must cover exactly three unique owned roles")

        signed_fills_by_role: dict[str, list[SignedFillEvidence]] = {
            role: [] for role in _ROLES
        }
        fill_ids: set[str] = set()
        venue_fill_by_id = {item.fill_id: item for item in bundle.fills}
        if len(venue_fill_by_id) != len(bundle.fills):
            raise StateConflict("bundle contains duplicate VenueFill identities")
        for fill in bundle.signed_fills:
            if (
                fill.fill_id != canonical_hyperliquid_fill_id(fill)
                or fill.fill_id in fill_ids
                or fill.role not in owned_by_role
            ):
                raise StateConflict("bundle contains duplicate or foreign signed fill")
            fill_ids.add(fill.fill_id)
            leg = owned_by_role[fill.role]
            if (
                fill.cloid != leg.cloid
                or fill.symbol != leg.symbol
                or fill.side is not leg.side
                or abs(fill.signed_quantity) != fill.quantity
                or (fill.signed_quantity > 0) != (fill.side is OrderSide.BUY)
                or _sum_decimal(
                    [fill.start_position, fill.signed_quantity]
                )
                != fill.end_position
                or fill.time_ms > snapshot.server_time_ms
            ):
                raise StateConflict("signed fill differs from owned leg or position chain")
            projection = venue_fill_by_id.get(fill.fill_id)
            if projection is None or projection != VenueFill(
                fill_id=fill.fill_id,
                role=fill.role,
                cloid=fill.cloid,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                occurred_at=_EPOCH + timedelta(milliseconds=fill.time_ms),
                venue_oid=fill.oid,
                venue_trade_id=fill.tid,
                transaction_hash=fill.transaction_hash,
                closed_pnl=fill.closed_pnl,
                fee_token=fill.fee_token,
                observed_at=_EPOCH + timedelta(
                    milliseconds=snapshot.server_time_ms
                ),
            ):
                raise StateConflict("VenueFill projection differs from signed fill evidence")
            signed_fills_by_role[fill.role].append(fill)
        if set(venue_fill_by_id) != fill_ids:
            raise StateConflict("bundle VenueFill and signed-fill sets differ")
        symbol = owned[0].symbol
        expected_auxiliary = {
            item.owner_id: item
            for item in _venue_module._durable_recovery_close_orders(
                self.store,
                parent_command_id=bundle.command_id,
                symbol=symbol,
            )
        }
        observed_auxiliary = {
            item.order.owner_id: item
            for item in bundle.auxiliary_order_statuses
        }
        if (
            len(observed_auxiliary) != len(bundle.auxiliary_order_statuses)
            or set(observed_auxiliary) != set(expected_auxiliary)
        ):
            raise StateConflict(
                "bundle auxiliary order set differs from durable recoveries"
            )
        auxiliary_status_oids: dict[int, object] = {}
        auxiliary_missing_unsettled = False
        auxiliary_status_nonterminal = False
        for owner_id, evidence in observed_auxiliary.items():
            expected_order = expected_auxiliary[owner_id]
            status = evidence.status
            if (
                evidence.order != expected_order
                or status.requested_cloid != expected_order.cloid
                or (
                    status.status_timestamp_ms is not None
                    and status.status_timestamp_ms > snapshot.server_time_ms
                )
            ):
                raise StateConflict(
                    "bundle auxiliary status differs from durable recovery"
                )
            if status.state is VenueOrderState.ORDER:
                if (
                    status.oid is None
                    or status.symbol != expected_order.symbol
                    or status.original_size != expected_order.requested_quantity
                    or status.is_trigger is not expected_order.is_trigger
                    or status.reduce_only is not expected_order.reduce_only
                ):
                    raise StateConflict(
                        "bundle auxiliary order semantics are inconsistent"
                    )
                if status.oid in auxiliary_status_oids:
                    raise StateConflict("bundle auxiliary venue OID is repeated")
                auxiliary_status_oids[status.oid] = evidence
                if (
                    status.venue_status
                    not in _venue_module._AUXILIARY_TERMINAL_STATUSES
                ):
                    auxiliary_status_nonterminal = True
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
                raise StateConflict("missing auxiliary order retains venue fields")
            elif (
                expected_order.expires_after_ms is not None
                and bundle.fill_coverage.requested_end_time_ms
                < expected_order.expires_after_ms
                + _venue_module._LATE_WRITE_SETTLEMENT_MS
            ):
                auxiliary_missing_unsettled = True
        primary_oids = {
            item.oid for item in bundle.order_statuses if item.oid is not None
        }
        if primary_oids & set(auxiliary_status_oids):
            raise StateConflict("parent and recovery orders share a venue OID")
        auxiliary_by_owner: dict[str, list[SignedFillEvidence]] = {
            owner_id: [] for owner_id in expected_auxiliary
        }
        persisted_auxiliary: dict[str, tuple[object, str]] = {}
        for persisted in self.store.list_recovery_fills(
            parent_command_id=bundle.command_id
        ):
            owner = self.store.get_recovery_command(
                persisted.recovery_command_id
            )
            occurred_ms = _datetime_ms(persisted.occurred_at)
            if (
                owner.state == "terminal"
                and bundle.fill_coverage.requested_start_time_ms
                <= occurred_ms
                <= bundle.fill_coverage.requested_end_time_ms
            ):
                persisted_auxiliary[persisted.fill_id] = (
                    persisted,
                    owner.recovery_hash,
                )
        observed_persisted: set[str] = set()
        for attributed in bundle.auxiliary_fills:
            fill = attributed.fill
            if fill.fill_id in fill_ids:
                raise StateConflict("bundle repeats a fill across ownership lanes")
            fill_ids.add(fill.fill_id)
            expected_order = expected_auxiliary.get(attributed.owner_id)
            status_evidence = auxiliary_status_oids.get(fill.oid)
            persisted_match = persisted_auxiliary.get(fill.fill_id)
            persisted_valid = False
            if persisted_match is not None:
                persisted, recovery_hash = persisted_match
                persisted_valid = (
                    attributed.owner_id == persisted.recovery_command_id
                    and attributed.source_hash == recovery_hash
                    and fill.role == "recovery_close"
                    and fill.cloid == persisted.cloid
                    and fill.symbol == persisted.symbol
                    and fill.side.value == persisted.side
                    and fill.quantity == persisted.quantity
                    and fill.signed_quantity == persisted.signed_quantity
                    and fill.start_position == persisted.start_position
                    and fill.end_position == persisted.end_position
                    and fill.price == persisted.price
                    and fill.fee == persisted.fee
                    and fill.closed_pnl == persisted.closed_pnl
                    and fill.fee_token == persisted.fee_token
                    and fill.crossed is persisted.crossed
                    and fill.builder_fee == persisted.builder_fee
                    and fill.oid == persisted.venue_oid
                    and fill.tid == persisted.venue_trade_id
                    and fill.transaction_hash == persisted.transaction_hash
                    and fill.time_ms == _datetime_ms(persisted.occurred_at)
                )
            if (
                fill.fill_id != canonical_hyperliquid_fill_id(fill)
                or attributed.owner_kind != "recovery_close"
                or (
                    not persisted_valid
                    and (
                        expected_order is None
                        or attributed.source_hash != expected_order.source_hash
                        or status_evidence is None
                        or getattr(status_evidence, "order") != expected_order
                        or fill.role != "recovery_close"
                        or fill.cloid != expected_order.cloid
                        or fill.symbol != expected_order.symbol
                        or fill.side is not expected_order.side
                    )
                )
                or fill.quantity != abs(fill.signed_quantity)
                or fill.end_position != fill.start_position + fill.signed_quantity
                or fill.time_ms > snapshot.server_time_ms
            ):
                raise StateConflict(
                    "bundle auxiliary fill differs from durable recovery"
                )
            if persisted_valid:
                observed_persisted.add(fill.fill_id)
            else:
                auxiliary_by_owner[attributed.owner_id].append(fill)
        if observed_persisted != set(persisted_auxiliary):
            raise StateConflict(
                "bundle omits a durable recovery fill in its covered window"
            )
        for owner_id, fills_for_owner in auxiliary_by_owner.items():
            expected_order = expected_auxiliary[owner_id]
            if _sum_decimal([item.quantity for item in fills_for_owner]) > (
                expected_order.requested_quantity
            ):
                raise StateConflict("auxiliary fills exceed recovery close size")
        auxiliary_fill_detail_missing = any(
            observed_auxiliary[owner_id].status.state is VenueOrderState.ORDER
            and observed_auxiliary[owner_id].status.venue_status == "filled"
            and _sum_decimal([item.quantity for item in fills_for_owner])
            != expected_auxiliary[owner_id].requested_quantity
            for owner_id, fills_for_owner in auxiliary_by_owner.items()
        )
        ordered_fills = sorted(
            [
                *bundle.signed_fills,
                *(item.fill for item in bundle.auxiliary_fills),
            ],
            key=lambda item: (item.time_ms, item.tid, item.oid),
        )
        discontinuous_fill_chain = any(
            left.end_position != right.start_position
            for left, right in zip(ordered_fills, ordered_fills[1:])
        )
        fill_chain_not_from_flat = bool(
            ordered_fills and ordered_fills[0].start_position != _ZERO
        )

        for role in _ROLES:
            leg = owned_by_role[role]
            status = statuses[role]
            update = updates[role]
            if (
                status.requested_cloid != leg.cloid
                or update.cloid != leg.cloid
                or status.role != role
                or update.role != role
            ):
                raise StateConflict("bundle order/CLOID differs from durable leg")
            if status.state is VenueOrderState.ORDER and (
                status.symbol != leg.symbol
                or status.original_size != leg.requested_quantity
                or status.remaining_size is None
                or status.remaining_size > leg.requested_quantity
                or status.reduce_only is not (role != "entry")
                or status.is_trigger is not (role != "entry")
            ):
                raise StateConflict("venue order status differs from owned leg semantics")
            if (
                status.status_timestamp_ms is not None
                and status.status_timestamp_ms > snapshot.server_time_ms
            ):
                raise StateConflict("venue order status postdates account snapshot")
            cumulative = _sum_decimal(
                [item.quantity for item in signed_fills_by_role[role]]
            )
            expected_status = _venue_module._leg_status(status, leg, cumulative)
            expected_cumulative = (
                leg.requested_quantity
                if status.venue_status == "filled"
                and cumulative != leg.requested_quantity
                else cumulative
            )
            if (
                update.status != expected_status
                or update.cumulative_filled != expected_cumulative
                or update.venue_oid != status.oid
            ):
                raise StateConflict("store leg update is not derived from venue evidence")

        coverage = bundle.fill_coverage
        preflight = self.store.get_preflight(bundle.command_id)
        if preflight.account_server_time_ms is None:
            raise StateConflict(
                "parent reconciliation lacks venue-server fill watermark"
            )
        expected_fill_start_ms = preflight.account_server_time_ms
        if coverage.requested_start_time_ms != expected_fill_start_ms:
            raise StateConflict(
                "fill coverage does not start at the flat preflight watermark"
            )
        coverage_hazard = (
            not coverage.complete
            or coverage.page_saturated
            or coverage.retention_limited
            or coverage.unmatched_fills != 0
        )
        if coverage.unique_fills != (
            len(bundle.signed_fills)
            + len(bundle.auxiliary_fills)
            + coverage.unmatched_fills
        ):
            raise StateConflict("fill coverage counts disagree with signed evidence")
        if coverage.requested_end_time_ms > snapshot.server_time_ms:
            raise StateConflict("fill coverage ends after account snapshot")
        if bundle.complete != (not bundle.incomplete_reasons):
            raise StateConflict("bundle complete flag and reasons disagree")
        missing_roles = tuple(
            role
            for role, status in statuses.items()
            if status.state is VenueOrderState.MISSING
        )
        fence: NoopFenceResolution | None = None
        if missing_roles:
            try:
                candidate = self.store.require_terminal_noop_fence(
                    bundle.command_id
                )
            except (RecordNotFound, StateConflict):
                candidate = None
            if candidate is not None and candidate.observed_at <= bundle.observed_at:
                fence = candidate
        fenced_missing_reasons = {
            f"{role}_order_missing" for role in _ROLES
        }
        fenced_absence = (
            fence is not None
            and set(missing_roles) == set(_ROLES)
            and not bundle.signed_fills
            and bundle.signed_position_quantity == _ZERO
            and bundle.protected_quantity == _ZERO
            and not coverage_hazard
            and set(bundle.incomplete_reasons) == fenced_missing_reasons
        )
        effective_complete = bundle.complete or fenced_absence
        if effective_complete and (
            coverage.requested_end_time_ms != snapshot.server_time_ms
        ):
            raise StateConflict(
                "complete fill coverage must end at the account snapshot"
            )
        if effective_complete and coverage_hazard:
            raise StateConflict("saturated or incomplete fill coverage cannot be complete")

        position = snapshot.position(symbol)
        signed_position = _ZERO if position is None else position.signed_size
        stop_cloid = owned_by_role["protective_stop"].cloid
        protection = snapshot.protection_coverage(
            symbol, expected_stop_cloids=(stop_cloid,)
        )
        if (
            bundle.signed_position_quantity != signed_position
            or bundle.protected_quantity != protection.covered_size
        ):
            raise StateConflict("bundle position/protection differs from account snapshot")
        account = snapshot.reconcile(
            owned_cloids=tuple(item.cloid for item in owned),
            allowed_position_symbols=(symbol,),
            expected_stop_cloids_by_symbol={symbol: (stop_cloid,)},
        )
        account_hazards = bool(
            account.foreign_order_oids
            or account.foreign_position_symbols
            or account.orphan_protection_oids
            or any(not item.fully_protected for item in account.protection)
        )
        summary_contradiction = (
            signed_position == _ZERO
            and (
                snapshot.margin_summary.total_notional_position != _ZERO
                or snapshot.margin_summary.total_margin_used != _ZERO
            )
        )
        completed_fill_detail_missing = any(
            status.venue_status == "filled"
            and _sum_decimal(
                [item.quantity for item in signed_fills_by_role[role]]
            )
            != owned_by_role[role].requested_quantity
            for role, status in statuses.items()
        )
        if effective_complete and (
            account_hazards
            or summary_contradiction
            or discontinuous_fill_chain
            or fill_chain_not_from_flat
            or completed_fill_detail_missing
            or auxiliary_fill_detail_missing
            or auxiliary_missing_unsettled
            or auxiliary_status_nonterminal
            or (
                any(
                    item.state is VenueOrderState.MISSING
                    for item in statuses.values()
                )
                and not fenced_absence
            )
            or (
                bundle.signed_position_quantity != _ZERO
                and not ordered_fills
            )
        ):
            raise StateConflict(
                "foreign, missing, discontinuous, or unsafe state cannot be complete"
            )
        if (
            effective_complete
            and ordered_fills
            and coverage.unmatched_fills == 0
            and ordered_fills[-1].end_position != signed_position
        ):
            raise StateConflict("complete fill chain does not reach account position")
        return effective_complete, fence if fenced_absence else None


__all__ = (
    "HyperliquidVenueReconciler",
    "MainEntryReconciliationCoordinator",
    "MainReconciliationResult",
)
