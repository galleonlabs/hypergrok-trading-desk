"""Strict read-only Hyperliquid venue reader for recovery reconciliation.

The recovery dispatcher is deliberately unable to manufacture venue truth.
This module accepts one immutable :class:`RecoveryCommand`, a separately
fetched account snapshot, and an injected ``/info`` transport.  It resolves
the exact recovery-owned CLOIDs with ``orderStatus`` and reads the account fill
window with ``userFillsByTime``.  Its only output is the typed
``RecoveryVenueRead`` consumed by the recovery reconciliation coordinator.

There is no signer, private key, nonce allocator, ``/exchange`` endpoint, or
persistence write in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import TypeAlias

from .canonical import canonical_json, domain_hash
from .errors import StateConflict, ValidationError
from .execution_store import ExecutionStore, RecoveryCommand
from .hyperliquid_account import HyperliquidAccountSnapshot, OrderSide
from . import hyperliquid_reconcile as _base
from .hyperliquid_reconcile import (
    AuxiliaryFillEvidence,
    AuxiliaryOrderEvidence,
    AuxiliaryOwnedOrder,
    FillCoverage,
    HyperliquidReconcileResponseError,
    InfoTransport,
    OwnedLeg,
    ParsedOrderStatus,
    SignedFillEvidence,
    VenueOrderState,
)
from .hyperliquid_recovery import (
    CancelByCloidAction,
    NoopFenceAction,
    RecoveryAction,
    ReduceOnlyCloseAction,
    ambiguous_attempt_hash,
    recovery_action_from_material,
)
from .market_data import post_public_info, public_info_endpoint
from .reconciliation_coordinator import _owned_legs, _verify_snapshot_hash
from .recovery_reconciliation import RecoveryVenueRead


Clock: TypeAlias = _base.Clock

USER_FILLS_PAGE_LIMIT = _base.USER_FILLS_PAGE_LIMIT
USER_FILLS_RETENTION_LIMIT = _base.USER_FILLS_RETENTION_LIMIT
MAX_FILL_PAGES = 6

_MAX_SNAPSHOT_AGE_MS = 5_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO = Decimal("0")
_NOOP_ROLES = ("entry", "protective_stop", "take_profit")


@dataclass(frozen=True, slots=True)
class _OrderSpec:
    role: str
    cloid: str
    symbol: str
    side: OrderSide | None
    quantity: Decimal | None
    is_trigger: bool | None
    reduce_only: bool | None


@dataclass(frozen=True, slots=True)
class _BoundStatus:
    parsed: ParsedOrderStatus
    spec: _OrderSpec
    side: OrderSide | None


def _request_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer millisecond timestamp")
    if not 0 <= value <= _base._MAX_TIMESTAMP_MS:
        raise ValidationError(f"{field} is outside supported bounds")
    return value


def _datetime_ms(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


def _decode_command(command: RecoveryCommand) -> RecoveryAction:
    if not isinstance(command, RecoveryCommand):
        raise TypeError("command must be an ExecutionStore RecoveryCommand")
    try:
        material = json.loads(command.recovery_material_json)
    except (TypeError, ValueError) as error:
        raise StateConflict("persisted recovery material is invalid JSON") from error
    if not isinstance(material, dict):
        raise StateConflict("persisted recovery material is not an object")
    try:
        encoded = canonical_json(material)
    except (TypeError, ValueError, RecursionError) as error:
        raise StateConflict("persisted recovery material is not canonical") from error
    if encoded != command.recovery_material_json:
        raise StateConflict("persisted recovery material JSON is not canonical")
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != command.recovery_material_hash:
        raise StateConflict("persisted recovery material hash differs")
    if domain_hash(
        "trading-harness/hyperliquid-recovery-action/v1", material
    ) != command.recovery_hash:
        raise StateConflict("persisted recovery action hash differs")
    try:
        action = recovery_action_from_material(material)
    except (TypeError, ValidationError) as error:
        raise StateConflict("persisted recovery material violates its schema") from error
    if (
        action.recovery_hash != command.recovery_hash
        or action.kind.value != command.kind
        or action.incident_id != command.incident_id
    ):
        raise StateConflict("recovery command bindings differ from its action")
    if isinstance(action, NoopFenceAction) and (
        action.command_id != command.parent_command_id
        or action.attempt_id != command.original_attempt_id
        or action.original_nonce != command.original_nonce
        or action.preflight_hash != command.preflight_hash
        or action.ambiguous_attempt_hash != command.source_hash
    ):
        raise StateConflict("noop command differs from its original attempt binding")
    if not isinstance(action, NoopFenceAction) and (
        command.original_attempt_id is not None or command.original_nonce is not None
    ):
        raise StateConflict("non-noop recovery unexpectedly binds an original attempt")
    return action


def _validate_snapshot(
    snapshot: HyperliquidAccountSnapshot,
    action: RecoveryAction,
    *,
    now_ms: int,
) -> str:
    if not isinstance(snapshot, HyperliquidAccountSnapshot):
        raise TypeError("snapshot must be HyperliquidAccountSnapshot")
    if action.network.value != "testnet" or snapshot.network != "testnet":
        raise ValidationError("recovery venue reading is testnet-only")
    endpoint = public_info_endpoint("testnet")
    if (
        snapshot.source_url != endpoint
        or snapshot.main_account_address != action.main_account_address
    ):
        raise StateConflict("account snapshot provenance differs from recovery action")
    _verify_snapshot_hash(snapshot)
    if (
        snapshot.received_at_ms < snapshot.server_time_ms
        or snapshot.received_at_ms > now_ms
        or snapshot.age_ms
        != snapshot.received_at_ms - snapshot.server_time_ms
        or now_ms - snapshot.server_time_ms > _MAX_SNAPSHOT_AGE_MS
    ):
        raise ValidationError("account snapshot is stale or future-dated")
    return endpoint


def _noop_specs(
    action: NoopFenceAction,
    store: ExecutionStore,
    snapshot: HyperliquidAccountSnapshot,
) -> tuple[_OrderSpec, ...]:
    parent = store.get_command(action.command_id)
    if parent.command_id != action.command_id:
        raise StateConflict("noop parent command lookup returned another command")
    attempt = store.get_attempt(action.command_id)
    if (
        attempt.attempt_id != action.attempt_id
        or attempt.command_id != action.command_id
        or attempt.preflight_hash != action.preflight_hash
        or attempt.signed_evidence_hash != action.signed_evidence_hash
        or attempt.transport_evidence_hash != action.transport_evidence_hash
        or attempt.nonce != action.original_nonce
        or attempt.action_hash != action.original_action_hash
        or attempt.wire_hash != action.original_wire_hash
        or attempt.state != "unknown"
        or ambiguous_attempt_hash(attempt) != action.ambiguous_attempt_hash
    ):
        raise StateConflict("noop recovery differs from the durable unknown attempt")
    legs = _owned_legs(store, parent.command_id, snapshot)
    if (
        len(legs) != 3
        or any(not isinstance(item, OwnedLeg) for item in legs)
        or {item.role for item in legs} != set(_NOOP_ROLES)
        or len({item.cloid for item in legs}) != 3
        or len({item.symbol for item in legs}) != 1
    ):
        raise StateConflict("durable parent is not an exact protected three-leg set")
    ordered = tuple(sorted(legs, key=lambda item: _NOOP_ROLES.index(item.role)))
    return tuple(
        _OrderSpec(
            role=leg.role,
            cloid=leg.cloid,
            symbol=leg.symbol,
            side=leg.side,
            quantity=leg.requested_quantity,
            is_trigger=leg.role != "entry",
            reduce_only=leg.role != "entry",
        )
        for leg in ordered
    )


def _order_specs(
    action: RecoveryAction,
    store: ExecutionStore,
    snapshot: HyperliquidAccountSnapshot,
) -> tuple[_OrderSpec, ...]:
    if isinstance(action, ReduceOnlyCloseAction):
        side = (
            OrderSide.SELL
            if action.original_signed_position > _ZERO
            else OrderSide.BUY
        )
        return (
            _OrderSpec(
                role="recovery_close",
                cloid=action.cloid,
                symbol=action.symbol,
                side=side,
                quantity=action.close_size,
                is_trigger=False,
                reduce_only=True,
            ),
        )
    if isinstance(action, CancelByCloidAction):
        return tuple(
            _OrderSpec(
                role=f"cancel_request_{index}",
                cloid=request.cloid,
                symbol=request.symbol,
                side=None,
                quantity=None,
                is_trigger=None,
                reduce_only=None,
            )
            for index, request in enumerate(action.requests)
        )
    return _noop_specs(action, store, snapshot)


def _parse_order_status(
    response: object,
    spec: _OrderSpec,
    *,
    cutoff_ms: int,
) -> _BoundStatus:
    root = _base._mapping(response, f"orderStatus[{spec.role}]")
    if root == {"status": "unknownOid"}:
        return _BoundStatus(
            ParsedOrderStatus(
                role=spec.role,
                requested_cloid=spec.cloid,
                state=VenueOrderState.MISSING,
                venue_status=None,
                status_timestamp_ms=None,
                oid=None,
                symbol=None,
                remaining_size=None,
                original_size=None,
                is_trigger=None,
                reduce_only=None,
            ),
            spec,
            None,
        )
    if set(root) != {"status", "order"} or root["status"] != "order":
        raise HyperliquidReconcileResponseError("orderStatus root is unsupported")
    outer = _base._mapping(root["order"], f"orderStatus[{spec.role}].order")
    if set(outer) != {"order", "status", "statusTimestamp"}:
        raise HyperliquidReconcileResponseError(
            "orderStatus record fields are unsupported"
        )
    venue_status = outer["status"]
    if not isinstance(venue_status, str) or venue_status not in _base._ORDER_STATUSES:
        raise HyperliquidReconcileResponseError("orderStatus venue status is unknown")
    status_time = _base._integer(
        outer["statusTimestamp"],
        f"orderStatus[{spec.role}].statusTimestamp",
    )
    if status_time > cutoff_ms:
        raise HyperliquidReconcileResponseError(
            "order status postdates the account snapshot"
        )
    order = _base._mapping(outer["order"], f"orderStatus[{spec.role}].order.order")
    if set(order) != _base._ORDER_FIELDS:
        raise HyperliquidReconcileResponseError("orderStatus order fields are unsupported")
    if order["cloid"] != spec.cloid:
        raise HyperliquidReconcileResponseError("orderStatus returned a foreign CLOID")
    if order["coin"] != spec.symbol:
        raise HyperliquidReconcileResponseError("orderStatus returned a foreign symbol")
    if order["side"] == "B":
        side = OrderSide.BUY
    elif order["side"] == "A":
        side = OrderSide.SELL
    else:
        raise HyperliquidReconcileResponseError("orderStatus side is unsupported")
    if spec.side is not None and side is not spec.side:
        raise HyperliquidReconcileResponseError("orderStatus side differs from recovery")
    remaining = _base._exact_decimal(
        order["sz"], "orderStatus.sz", nonnegative=True
    )
    original = _base._exact_decimal(
        order["origSz"], "orderStatus.origSz", nonnegative=True
    )
    position_tpsl = _base._bool(
        order["isPositionTpsl"], "orderStatus.isPositionTpsl"
    )
    if original == _ZERO and not position_tpsl:
        raise HyperliquidReconcileResponseError(
            "non-position order has zero original size"
        )
    if remaining > original:
        raise HyperliquidReconcileResponseError(
            "orderStatus remaining size exceeds original"
        )
    if spec.quantity is not None and original != spec.quantity:
        raise HyperliquidReconcileResponseError(
            "orderStatus original size differs from recovery"
        )
    is_trigger = _base._bool(order["isTrigger"], "orderStatus.isTrigger")
    reduce_only = _base._bool(order["reduceOnly"], "orderStatus.reduceOnly")
    if spec.is_trigger is not None and is_trigger is not spec.is_trigger:
        raise HyperliquidReconcileResponseError(
            "orderStatus trigger flag differs from recovery"
        )
    if spec.reduce_only is not None and reduce_only is not spec.reduce_only:
        raise HyperliquidReconcileResponseError(
            "orderStatus reduce-only flag differs from recovery"
        )
    _base._exact_decimal(order["limitPx"], "orderStatus.limitPx", positive=True)
    trigger_price = _base._exact_decimal(
        order["triggerPx"], "orderStatus.triggerPx", nonnegative=True
    )
    if is_trigger != (trigger_price > _ZERO):
        raise HyperliquidReconcileResponseError(
            "orderStatus trigger fields disagree"
        )
    oid = _base._integer(order["oid"], "orderStatus.oid", maximum=2**63 - 1)
    order_time = _base._integer(order["timestamp"], "orderStatus.timestamp")
    if order_time > status_time:
        raise HyperliquidReconcileResponseError("order timestamp postdates status")
    _base._text(order["triggerCondition"], "orderStatus.triggerCondition")
    _base._text(order["orderType"], "orderStatus.orderType")
    if order["tif"] is not None:
        _base._text(order["tif"], "orderStatus.tif", maximum=64)
    _base._array(order["children"], "orderStatus.children", maximum=20)
    return _BoundStatus(
        ParsedOrderStatus(
            role=spec.role,
            requested_cloid=spec.cloid,
            state=VenueOrderState.ORDER,
            venue_status=venue_status,
            status_timestamp_ms=status_time,
            oid=oid,
            symbol=spec.symbol,
            remaining_size=remaining,
            original_size=original,
            is_trigger=is_trigger,
            reduce_only=reduce_only,
        ),
        spec,
        side,
    )


def _fetch_fills(
    endpoint: str,
    account: str,
    *,
    start_time_ms: int,
    end_time_ms: int,
    now_ms: int,
    transport: InfoTransport,
) -> tuple[tuple[_base._RawFill, ...], FillCoverage]:
    cursor = start_time_ms
    by_identity: dict[tuple[int, str, int, int], _base._RawFill] = {}
    page_count = 0
    returned_rows = 0
    duplicate_fills = 0
    page_saturated = False
    retention_limited = False
    complete = False
    reason = "maximum_fill_pages_exhausted"
    required_overlap: set[tuple[int, str, int, int]] = set()
    while page_count < MAX_FILL_PAGES:
        response = _base._post_info(
            endpoint,
            {
                "type": "userFillsByTime",
                "user": account,
                "startTime": cursor,
                "endTime": end_time_ms,
                "aggregateByTime": False,
            },
            transport,
        )
        rows = _base._array(
            response,
            f"userFillsByTime page {page_count + 1}",
            maximum=USER_FILLS_PAGE_LIMIT,
        )
        page_count += 1
        returned_rows += len(rows)
        page: list[_base._RawFill] = []
        for index, raw in enumerate(rows):
            fill = _base._parse_fill(
                raw,
                index,
                start_time_ms=cursor,
                end_time_ms=end_time_ms,
                now_ms=now_ms,
            )
            if fill.time_ms < cursor:
                raise HyperliquidReconcileResponseError(
                    "userFillsByTime returned a fill before its inclusive cursor"
                )
            page.append(fill)
            prior = by_identity.get(fill.identity)
            if prior is None:
                by_identity[fill.identity] = fill
            elif prior == fill:
                duplicate_fills += 1
            else:
                raise HyperliquidReconcileResponseError(
                    "duplicate fill identity has conflicting economics"
                )
        if any(
            page[index].time_ms > page[index + 1].time_ms
            for index in range(len(page) - 1)
        ):
            raise HyperliquidReconcileResponseError(
                "userFillsByTime page is not ordered by ascending time"
            )
        page_identities = {item.identity for item in page}
        if required_overlap and not required_overlap.issubset(page_identities):
            raise HyperliquidReconcileResponseError(
                "userFillsByTime inclusive page overlap is incomplete"
            )
        if len(by_identity) >= USER_FILLS_RETENTION_LIMIT:
            retention_limited = True
            reason = "latest_10000_fill_retention_limit"
            break
        if len(rows) < USER_FILLS_PAGE_LIMIT:
            complete = True
            reason = "range_exhausted"
            break
        next_cursor = max(item.time_ms for item in page)
        if next_cursor <= cursor:
            page_saturated = True
            reason = "inclusive_page_boundary_saturated"
            break
        required_overlap = {
            item.identity for item in page if item.time_ms == next_cursor
        }
        cursor = next_cursor
    else:
        page_saturated = True
    ordered = tuple(by_identity[key] for key in sorted(by_identity))
    return ordered, FillCoverage(
        requested_start_time_ms=start_time_ms,
        requested_end_time_ms=end_time_ms,
        page_count=page_count,
        page_limit=USER_FILLS_PAGE_LIMIT,
        retention_limit=USER_FILLS_RETENTION_LIMIT,
        returned_rows=returned_rows,
        unique_fills=len(ordered),
        duplicate_fills=duplicate_fills,
        unmatched_fills=0,
        page_saturated=page_saturated,
        retention_limited=retention_limited,
        complete=complete and not retention_limited and not page_saturated,
        reason=reason,
    )


def _bind_fills(
    raw_fills: tuple[_base._RawFill, ...],
    statuses: tuple[_BoundStatus, ...],
    coverage: FillCoverage,
    auxiliary_statuses: tuple[AuxiliaryOrderEvidence, ...] = (),
) -> tuple[
    tuple[SignedFillEvidence, ...],
    tuple[AuxiliaryFillEvidence, ...],
    FillCoverage,
]:
    by_oid = {
        item.parsed.oid: item
        for item in statuses
        if item.parsed.oid is not None
    }
    if len(by_oid) != sum(item.parsed.oid is not None for item in statuses):
        raise HyperliquidReconcileResponseError("orderStatus repeats a venue OID")
    signed: list[SignedFillEvidence] = []
    auxiliary: list[AuxiliaryFillEvidence] = []
    auxiliary_by_oid = {
        item.status.oid: item
        for item in auxiliary_statuses
        if item.status.oid is not None
    }
    if set(by_oid) & set(auxiliary_by_oid):
        raise HyperliquidReconcileResponseError(
            "target and auxiliary orderStatus share a venue OID"
        )
    unmatched = 0
    for fill in raw_fills:
        binding = by_oid.get(fill.oid)
        if binding is None:
            auxiliary_binding = auxiliary_by_oid.get(fill.oid)
            if auxiliary_binding is None:
                unmatched += 1
                continue
            owned = auxiliary_binding.order
            if fill.symbol != owned.symbol or fill.side is not owned.side:
                raise HyperliquidReconcileResponseError(
                    "fill differs from its auxiliary parent order"
                )
            auxiliary.append(
                AuxiliaryFillEvidence(
                    owner_kind=owned.owner_kind,
                    owner_id=owned.owner_id,
                    source_hash=owned.source_hash,
                    fill=SignedFillEvidence(
                        fill_id=(
                            f"hyperliquid:{fill.symbol}:{fill.time_ms}:"
                            f"{fill.tid}:{fill.oid}"
                        ),
                        role=owned.role,
                        cloid=owned.cloid,
                        oid=fill.oid,
                        tid=fill.tid,
                        transaction_hash=fill.transaction_hash,
                        symbol=fill.symbol,
                        side=fill.side,
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
                        time_ms=fill.time_ms,
                    ),
                )
            )
            continue
        if fill.symbol != binding.spec.symbol or fill.side is not binding.side:
            raise HyperliquidReconcileResponseError(
                "fill differs from its recovery-owned order"
            )
        fill_id = f"hyperliquid:{fill.symbol}:{fill.time_ms}:{fill.tid}:{fill.oid}"
        signed.append(
            SignedFillEvidence(
                fill_id=fill_id,
                role=binding.spec.role,
                cloid=binding.spec.cloid,
                oid=fill.oid,
                tid=fill.tid,
                transaction_hash=fill.transaction_hash,
                symbol=fill.symbol,
                side=fill.side,
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
                time_ms=fill.time_ms,
            )
        )
    complete = coverage.complete and unmatched == 0
    reason = coverage.reason if coverage.reason != "range_exhausted" else (
        "unmatched_account_fills" if unmatched else "range_exhausted"
    )
    bound_coverage = FillCoverage(
        requested_start_time_ms=coverage.requested_start_time_ms,
        requested_end_time_ms=coverage.requested_end_time_ms,
        page_count=coverage.page_count,
        page_limit=coverage.page_limit,
        retention_limit=coverage.retention_limit,
        returned_rows=coverage.returned_rows,
        unique_fills=coverage.unique_fills,
        duplicate_fills=coverage.duplicate_fills,
        unmatched_fills=unmatched,
        page_saturated=coverage.page_saturated,
        retention_limited=coverage.retention_limited,
        complete=complete,
        reason=reason,
    )
    return tuple(signed), tuple(auxiliary), bound_coverage


def read_recovery_venue(
    store: ExecutionStore,
    command: RecoveryCommand,
    snapshot: HyperliquidAccountSnapshot,
    *,
    fills_start_time_ms: int,
    transport: InfoTransport = post_public_info,
    clock: Clock = lambda: datetime.now(timezone.utc),
    fills_end_time_ms: int | None = None,
) -> RecoveryVenueRead:
    """Read exact testnet order/fill evidence for one recovery command.

    For ``noop_fence`` the immutable parent legs and unknown attempt are loaded
    from ``store``.  They are never accepted from the caller.
    """

    if not isinstance(store, ExecutionStore):
        raise TypeError("store must be ExecutionStore")
    if not isinstance(command, RecoveryCommand):
        raise TypeError("command must be an ExecutionStore RecoveryCommand")
    if not callable(transport) or not callable(clock):
        raise TypeError("transport and clock must be callable")
    persisted = store.get_recovery_command(command.recovery_command_id)
    if persisted != command:
        raise StateConflict("caller recovery command differs from durable state")
    action = _decode_command(command)
    if store.environment.value != "testnet" or store.account_id != action.account_id:
        raise StateConflict("execution store account differs from recovery action")
    now_ms = _base._clock_ms(clock)
    endpoint = _validate_snapshot(snapshot, action, now_ms=now_ms)
    start_ms = _request_integer(fills_start_time_ms, "fills_start_time_ms")
    end_ms = (
        snapshot.server_time_ms
        if fills_end_time_ms is None
        else _request_integer(fills_end_time_ms, "fills_end_time_ms")
    )
    if end_ms != snapshot.server_time_ms or start_ms > end_ms:
        raise ValidationError(
            "fill window must end exactly at the fresh account snapshot"
        )
    specs = _order_specs(action, store, snapshot)
    auxiliary_orders: tuple[AuxiliaryOwnedOrder, ...] = ()
    if isinstance(action, ReduceOnlyCloseAction):
        parent = store.get_command(command.parent_command_id)
        parent_legs = _owned_legs(store, command.parent_command_id, snapshot)
        auxiliary_orders = tuple(
            AuxiliaryOwnedOrder(
                owner_kind="parent_leg",
                owner_id=command.parent_command_id,
                source_hash=parent.plan_hash,
                role=leg.role,
                cloid=leg.cloid,
                symbol=leg.symbol,
                side=leg.side,
                requested_quantity=leg.requested_quantity,
                is_trigger=leg.role != "entry",
                reduce_only=leg.role != "entry",
            )
            for leg in parent_legs
        )
        if {item.cloid for item in auxiliary_orders} & {
            item.cloid for item in specs
        }:
            raise StateConflict("recovery close reuses a durable parent CLOID")
    bound_statuses = tuple(
        _parse_order_status(
            _base._post_info(
                endpoint,
                {
                    "type": "orderStatus",
                    "user": action.main_account_address,
                    "oid": spec.cloid,
                },
                transport,
            ),
            spec,
            cutoff_ms=end_ms,
        )
        for spec in specs
    )
    auxiliary_statuses = tuple(
        _base._parse_auxiliary_order_status(
            _base._post_info(
                endpoint,
                {
                    "type": "orderStatus",
                    "user": action.main_account_address,
                    "oid": order.cloid,
                },
                transport,
            ),
            order,
            now_ms=end_ms,
        )
        for order in auxiliary_orders
    )
    raw_fills, coverage = _fetch_fills(
        endpoint,
        action.main_account_address,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        now_ms=now_ms,
        transport=transport,
    )
    signed_fills, auxiliary_fills, bound_coverage = _bind_fills(
        raw_fills,
        bound_statuses,
        coverage,
        auxiliary_statuses,
    )
    finished_ms = _base._clock_ms(clock)
    if (
        finished_ms < now_ms
        or finished_ms - snapshot.server_time_ms > _MAX_SNAPSHOT_AGE_MS
    ):
        raise ValidationError(
            "recovery venue reads exceeded the fresh snapshot window"
        )
    return RecoveryVenueRead(
        network="testnet",
        account_id=action.account_id,
        account_snapshot_hash=snapshot.snapshot_hash,
        observed_at=_datetime_ms(finished_ms),
        order_statuses=tuple(item.parsed for item in bound_statuses),
        signed_fills=signed_fills,
        fill_coverage=bound_coverage,
        auxiliary_order_statuses=auxiliary_statuses,
        auxiliary_fills=auxiliary_fills,
    )


@dataclass(frozen=True, slots=True)
class HyperliquidRecoveryVenueReader:
    """Store-bound recovery reader with injected, read-only venue I/O."""

    store: ExecutionStore
    transport: InfoTransport = post_public_info
    clock: Clock = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if not isinstance(self.store, ExecutionStore):
            raise TypeError("store must be ExecutionStore")
        if self.store.environment.value != "testnet":
            raise ValidationError("recovery venue reader is testnet-only")
        if not callable(self.transport) or not callable(self.clock):
            raise TypeError("transport and clock must be callable")

    def read(
        self,
        command: RecoveryCommand,
        snapshot: HyperliquidAccountSnapshot,
        *,
        fills_start_time_ms: int,
        fills_end_time_ms: int | None = None,
    ) -> RecoveryVenueRead:
        return read_recovery_venue(
            self.store,
            command,
            snapshot,
            fills_start_time_ms=fills_start_time_ms,
            fills_end_time_ms=fills_end_time_ms,
            transport=self.transport,
            clock=self.clock,
        )


__all__ = (
    "HyperliquidRecoveryVenueReader",
    "MAX_FILL_PAGES",
    "USER_FILLS_PAGE_LIMIT",
    "USER_FILLS_RETENTION_LIMIT",
    "read_recovery_venue",
)
