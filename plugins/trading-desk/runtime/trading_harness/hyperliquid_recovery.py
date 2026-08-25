"""Typed construction of the only Hyperliquid account-safety actions.

Recovery actions are not general exchange actions.  They are immutable,
incident-bound requests derived from fresh venue or durable ambiguity state:

* a bounded reduce-only IOC that cannot exceed or flip a fresh signed
  position;
* ``cancelByCloid`` for explicitly owned CLOIDs; and
* a ``noop`` using the *same* nonce as a durably persisted unknown attempt.

This module does not sign or transmit.  The signer independently validates the
result again before it can access an injected wallet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import TypeAlias

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .errors import ValidationError
from .execution_store import AttemptRecord, IncidentRecord
from .hyperliquid_account import HyperliquidAccountSnapshot, PositionSide
from .hyperliquid_wire import (
    HyperliquidNetwork,
    format_perp_price,
    format_perp_size,
)


RECOVERY_ACTION_HASH_DOMAIN = "trading-harness/hyperliquid-recovery-action/v1"
AMBIGUOUS_ATTEMPT_HASH_DOMAIN = "trading-harness/hyperliquid-ambiguous-attempt/v1"
RECOVERY_CLOSE_CLOID_DOMAIN = (
    "trading-harness/hyperliquid-recovery-close-cloid/v1"
)

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_ZERO = Decimal("0")
_MAX_RECOVERY_EXPIRY_MS = 15_000
_MAX_SNAPSHOT_AGE_MS = 5_000
_MAX_FUTURE_SKEW_MS = 5_000
_MAX_CANCELS = 20
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RecoveryKind(str, Enum):
    REDUCE_ONLY_CLOSE = "reduce_only_close"
    CANCEL_BY_CLOID = "cancel_by_cloid"
    NOOP_FENCE = "noop_fence"


@dataclass(frozen=True, slots=True)
class CancelRequest:
    symbol: str
    cloid: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValidationError("cancel symbol must be canonical")
        if not isinstance(self.cloid, str) or not _CLOID_RE.fullmatch(self.cloid):
            raise ValidationError("cancel CLOID must be a lowercase 128-bit value")


@dataclass(frozen=True, slots=True)
class ReduceOnlyCloseAction:
    kind: RecoveryKind
    network: HyperliquidNetwork
    account_id: str
    main_account_address: str
    incident_id: str
    position_snapshot_hash: str
    symbol: str
    asset_id: int
    original_signed_position: Decimal
    close_size: Decimal
    price_bound: Decimal
    cloid: str
    expires_at_ms: int
    action: dict[str, object]
    recovery_hash: str
    position_snapshot_time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CancelByCloidAction:
    kind: RecoveryKind
    network: HyperliquidNetwork
    account_id: str
    main_account_address: str
    incident_id: str
    account_snapshot_hash: str
    requests: tuple[CancelRequest, ...]
    asset_ids: tuple[int, ...]
    expires_at_ms: int
    action: dict[str, object]
    recovery_hash: str
    account_snapshot_time_ms: int | None = None


@dataclass(frozen=True, slots=True)
class NoopFenceAction:
    kind: RecoveryKind
    network: HyperliquidNetwork
    account_id: str
    main_account_address: str
    incident_id: str
    attempt_id: str
    command_id: str
    preflight_hash: str | None
    signed_evidence_hash: str | None
    transport_evidence_hash: str | None
    original_nonce: int
    original_action_hash: str
    original_wire_hash: str
    ambiguous_attempt_hash: str
    expires_at_ms: int
    action: dict[str, object]
    recovery_hash: str


RecoveryAction: TypeAlias = (
    ReduceOnlyCloseAction | CancelByCloidAction | NoopFenceAction
)


def _material_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
        validate_decimal_bounds(parsed, field=field)
    except (ArithmeticError, ValueError) as error:
        raise ValidationError(f"{field} is not a bounded decimal") from error
    if canonical_decimal(parsed) != value:
        raise ValidationError(f"{field} is not canonically encoded")
    return parsed


def recovery_action_from_material(value: Mapping[str, object]) -> RecoveryAction:
    """Strictly reconstruct one persisted recovery artifact after restart."""

    if not isinstance(value, Mapping):
        raise TypeError("recovery material must be a mapping")
    material = dict(value)
    kind_value = material.get("kind")
    try:
        kind = RecoveryKind(kind_value)
    except (TypeError, ValueError) as error:
        raise ValidationError("recovery material kind is invalid") from error
    common_keys = {
        "kind",
        "network",
        "account_id",
        "main_account_address",
        "incident_id",
        "expires_at_ms",
        "action",
    }
    expected_extra = {
        RecoveryKind.REDUCE_ONLY_CLOSE: {
            "position_snapshot_hash",
            "symbol",
            "asset_id",
            "original_signed_position",
            "close_size",
            "price_bound",
            "cloid",
        },
        RecoveryKind.CANCEL_BY_CLOID: {"account_snapshot_hash", "requests"},
        RecoveryKind.NOOP_FENCE: {
            "attempt_id",
            "command_id",
            "preflight_hash",
            "signed_evidence_hash",
            "transport_evidence_hash",
            "original_nonce",
            "original_action_hash",
            "original_wire_hash",
            "ambiguous_attempt_hash",
        },
    }[kind]
    allowed_fields = common_keys | expected_extra
    if kind is RecoveryKind.REDUCE_ONLY_CLOSE and "position_snapshot_time_ms" in material:
        allowed_fields = allowed_fields | {"position_snapshot_time_ms"}
    if kind is RecoveryKind.CANCEL_BY_CLOID and "account_snapshot_time_ms" in material:
        allowed_fields = allowed_fields | {"account_snapshot_time_ms"}
    if set(material) != allowed_fields:
        raise ValidationError("recovery material fields are unsupported")
    network = _network(material["network"])
    account_id = _text(material["account_id"], "account_id")
    main_account = _address(material["main_account_address"], "main_account_address")
    incident_id = _text(material["incident_id"], "incident_id")
    expires_at_ms = material["expires_at_ms"]
    if type(expires_at_ms) is not int or expires_at_ms < 0:
        raise ValidationError("recovery expires_at_ms is invalid")
    action_value = material["action"]
    if not isinstance(action_value, dict) or not all(
        isinstance(key, str) for key in action_value
    ):
        raise ValidationError("recovery action must be an object")
    action = deepcopy(action_value)
    recovery_hash = _recovery_hash(material)
    if kind is RecoveryKind.REDUCE_ONLY_CLOSE:
        snapshot_time = material.get("position_snapshot_time_ms")
        if snapshot_time is not None and (
            type(snapshot_time) is not int or snapshot_time < 0
        ):
            raise ValidationError("recovery position snapshot time is invalid")
        asset_id = material["asset_id"]
        if type(asset_id) is not int or not 0 <= asset_id <= 1_000_000:
            raise ValidationError("recovery asset_id is invalid")
        original = _material_decimal(
            material["original_signed_position"],
            "original_signed_position",
        )
        close_size = _material_decimal(material["close_size"], "close_size")
        price_bound = _material_decimal(material["price_bound"], "price_bound")
        if original == _ZERO or close_size <= _ZERO or close_size > abs(original):
            raise ValidationError("recovery close economics are invalid")
        if price_bound <= _ZERO:
            raise ValidationError("recovery price bound must be positive")
        result: RecoveryAction = ReduceOnlyCloseAction(
            kind=kind,
            network=network,
            account_id=account_id,
            main_account_address=main_account,
            incident_id=incident_id,
            position_snapshot_hash=_hash(
                material["position_snapshot_hash"], "position_snapshot_hash"
            ),
            symbol=_text(material["symbol"], "symbol", maximum=64),
            asset_id=asset_id,
            original_signed_position=original,
            close_size=close_size,
            price_bound=price_bound,
            cloid=_cloid(material["cloid"], "close cloid"),
            expires_at_ms=expires_at_ms,
            action=action,
            recovery_hash=recovery_hash,
            position_snapshot_time_ms=snapshot_time,
        )
    elif kind is RecoveryKind.CANCEL_BY_CLOID:
        snapshot_time = material.get("account_snapshot_time_ms")
        if snapshot_time is not None and (
            type(snapshot_time) is not int or snapshot_time < 0
        ):
            raise ValidationError("recovery account snapshot time is invalid")
        raw_requests = material["requests"]
        if not isinstance(raw_requests, list) or not 1 <= len(raw_requests) <= _MAX_CANCELS:
            raise ValidationError("recovery cancel requests are invalid")
        requests: list[CancelRequest] = []
        asset_ids: list[int] = []
        for index, raw in enumerate(raw_requests):
            if not isinstance(raw, dict) or set(raw) != {
                "symbol",
                "asset_id",
                "cloid",
            }:
                raise ValidationError(f"recovery cancel request {index} is invalid")
            asset_id = raw["asset_id"]
            if type(asset_id) is not int or not 0 <= asset_id <= 1_000_000:
                raise ValidationError("recovery cancel asset_id is invalid")
            requests.append(CancelRequest(raw["symbol"], raw["cloid"]))
            asset_ids.append(asset_id)
        result = CancelByCloidAction(
            kind=kind,
            network=network,
            account_id=account_id,
            main_account_address=main_account,
            incident_id=incident_id,
            account_snapshot_hash=_hash(
                material["account_snapshot_hash"], "account_snapshot_hash"
            ),
            requests=tuple(requests),
            asset_ids=tuple(asset_ids),
            expires_at_ms=expires_at_ms,
            action=action,
            recovery_hash=recovery_hash,
            account_snapshot_time_ms=snapshot_time,
        )
    else:
        original_nonce = material["original_nonce"]
        if type(original_nonce) is not int or original_nonce < 0:
            raise ValidationError("noop original_nonce is invalid")
        result = NoopFenceAction(
            kind=kind,
            network=network,
            account_id=account_id,
            main_account_address=main_account,
            incident_id=incident_id,
            attempt_id=_text(material["attempt_id"], "attempt_id"),
            command_id=_text(material["command_id"], "command_id"),
            preflight_hash=_hash(material["preflight_hash"], "preflight_hash"),
            signed_evidence_hash=_hash(
                material["signed_evidence_hash"], "signed_evidence_hash"
            ),
            transport_evidence_hash=_hash(
                material["transport_evidence_hash"], "transport_evidence_hash"
            ),
            original_nonce=original_nonce,
            original_action_hash=_hash(
                material["original_action_hash"], "original_action_hash"
            ),
            original_wire_hash=_hash(
                material["original_wire_hash"], "original_wire_hash"
            ),
            ambiguous_attempt_hash=_hash(
                material["ambiguous_attempt_hash"], "ambiguous_attempt_hash"
            ),
            expires_at_ms=expires_at_ms,
            action=action,
            recovery_hash=recovery_hash,
        )
    if recovery_action_material(result) != material:
        raise ValidationError("decoded recovery material does not round trip")
    return result


def recovery_action_material(recovery: RecoveryAction) -> dict[str, object]:
    """Return the exact canonical material committed by ``recovery_hash``.

    Recovery permits persist this material so restart-time reconciliation can
    prove concrete quantities and CLOIDs rather than trusting an opaque digest.
    The returned action is a deep copy; callers cannot mutate the reviewed
    artifact through this helper.
    """

    common: dict[str, object] = {
        "kind": recovery.kind.value,
        "network": recovery.network.value,
        "account_id": recovery.account_id,
        "main_account_address": recovery.main_account_address,
        "incident_id": recovery.incident_id,
    }
    if isinstance(recovery, ReduceOnlyCloseAction):
        material = {
            **common,
            "position_snapshot_hash": recovery.position_snapshot_hash,
            "symbol": recovery.symbol,
            "asset_id": recovery.asset_id,
            "original_signed_position": canonical_decimal(
                recovery.original_signed_position
            ),
            "close_size": canonical_decimal(recovery.close_size),
            "price_bound": canonical_decimal(recovery.price_bound),
            "cloid": recovery.cloid,
            "expires_at_ms": recovery.expires_at_ms,
            "action": deepcopy(recovery.action),
        }
        if recovery.position_snapshot_time_ms is not None:
            material["position_snapshot_time_ms"] = (
                recovery.position_snapshot_time_ms
            )
    elif isinstance(recovery, CancelByCloidAction):
        material = {
            **common,
            "account_snapshot_hash": recovery.account_snapshot_hash,
            "requests": [
                {
                    "symbol": request.symbol,
                    "asset_id": asset_id,
                    "cloid": request.cloid,
                }
                for request, asset_id in zip(recovery.requests, recovery.asset_ids)
            ],
            "expires_at_ms": recovery.expires_at_ms,
            "action": deepcopy(recovery.action),
        }
        if recovery.account_snapshot_time_ms is not None:
            material["account_snapshot_time_ms"] = (
                recovery.account_snapshot_time_ms
            )
    elif isinstance(recovery, NoopFenceAction):
        material = {
            **common,
            "attempt_id": recovery.attempt_id,
            "command_id": recovery.command_id,
            "preflight_hash": recovery.preflight_hash,
            "signed_evidence_hash": recovery.signed_evidence_hash,
            "transport_evidence_hash": recovery.transport_evidence_hash,
            "original_nonce": recovery.original_nonce,
            "original_action_hash": recovery.original_action_hash,
            "original_wire_hash": recovery.original_wire_hash,
            "ambiguous_attempt_hash": recovery.ambiguous_attempt_hash,
            "expires_at_ms": recovery.expires_at_ms,
            "action": deepcopy(recovery.action),
        }
    else:
        raise TypeError("recovery must be a typed RecoveryAction")
    if _recovery_hash(material) != recovery.recovery_hash:
        raise ValidationError("recovery hash does not match its exact material")
    return material


def _text(value: object, field: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError(f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _address(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase 20-byte address")
    return value


def _cloid(value: object, field: str) -> str:
    if not isinstance(value, str) or not _CLOID_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase 128-bit CLOID")
    return value


def _exact_positive(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    validate_decimal_bounds(value, field=field)
    if value <= _ZERO:
        raise ValidationError(f"{field} must be positive")
    return value


def _utc_ms(value: datetime, field: str) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    delta = utc - _EPOCH
    result = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if result < 0:
        raise ValidationError(f"{field} predates the Unix epoch")
    return result


def _network(value: object) -> HyperliquidNetwork:
    if isinstance(value, HyperliquidNetwork):
        return value
    try:
        return HyperliquidNetwork(value)
    except (TypeError, ValueError) as error:
        raise ValidationError("network must be explicit mainnet or testnet") from error


def _active_incident(incident: IncidentRecord) -> IncidentRecord:
    if not isinstance(incident, IncidentRecord):
        raise TypeError("incident must be a persisted IncidentRecord")
    _text(incident.incident_id, "incident_id")
    if incident.state != "open":
        raise ValidationError("recovery requires an open persisted incident")
    return incident


def _expiry(at_ms: int, ttl_ms: int) -> int:
    if type(ttl_ms) is not int or not 1_000 <= ttl_ms <= _MAX_RECOVERY_EXPIRY_MS:
        raise ValidationError("recovery ttl_ms must be from 1000 to 15000")
    return at_ms + ttl_ms


def _fresh_snapshot(
    snapshot: HyperliquidAccountSnapshot,
    *,
    network: HyperliquidNetwork,
    at_ms: int,
) -> None:
    if not isinstance(snapshot, HyperliquidAccountSnapshot):
        raise TypeError("snapshot must be HyperliquidAccountSnapshot")
    if snapshot.network != network.value:
        raise ValidationError("account snapshot network does not match recovery network")
    age = at_ms - snapshot.server_time_ms
    if age > _MAX_SNAPSHOT_AGE_MS or age < -_MAX_FUTURE_SKEW_MS:
        raise ValidationError("account snapshot is not fresh enough for recovery")
    _hash(snapshot.snapshot_hash, "account snapshot hash")


def _recovery_hash(material: dict[str, object]) -> str:
    return domain_hash(RECOVERY_ACTION_HASH_DOMAIN, material)


def derive_recovery_close_cloid(
    *,
    account_id: str,
    incident_id: str,
    position_snapshot_hash: str,
) -> str:
    """Derive one 128-bit close CLOID from immutable recovery source truth.

    A new fresh account snapshot necessarily produces a new identifier, so a
    definitive failed or partial close cannot exhaust a finite configured
    CLOID pool.  The controller still rejects a cryptographic truncation
    collision against durable and currently open identifiers before issuing
    authority, and the live signer independently recomputes this value.
    """

    material = {
        "account_id": _text(account_id, "account_id"),
        "incident_id": _text(incident_id, "incident_id"),
        "position_snapshot_hash": _hash(
            position_snapshot_hash,
            "position_snapshot_hash",
        ),
    }
    return "0x" + domain_hash(RECOVERY_CLOSE_CLOID_DOMAIN, material)[:32]


def build_reduce_only_close(
    snapshot: HyperliquidAccountSnapshot,
    *,
    symbol: str,
    price_bound: Decimal,
    cloid: str,
    incident: IncidentRecord,
    account_id: str,
    network: HyperliquidNetwork,
    at: datetime,
    close_size: Decimal | None = None,
    ttl_ms: int = 10_000,
) -> ReduceOnlyCloseAction:
    """Build a bounded IOC whose size cannot exceed the fresh signed position."""

    selected_network = _network(network)
    at_ms = _utc_ms(at, "at")
    _fresh_snapshot(snapshot, network=selected_network, at_ms=at_ms)
    selected_incident = _active_incident(incident)
    checked_account = _text(account_id, "account_id")
    main_address = _address(snapshot.main_account_address, "main_account_address")
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise ValidationError("symbol must be canonical")
    position = snapshot.position(symbol)
    if position is None:
        raise ValidationError("cannot close an absent position")
    requested = (
        position.absolute_size
        if close_size is None
        else _exact_positive(close_size, "close_size")
    )
    if requested > position.absolute_size:
        raise ValidationError("close_size exceeds the fresh position and could flip")
    bound = _exact_positive(price_bound, "price_bound")
    checked_cloid = _cloid(cloid, "close cloid")
    instrument = snapshot.metadata.instrument(symbol)
    size_wire = format_perp_size(requested, sz_decimals=instrument.sz_decimals)
    price_wire = format_perp_price(bound, sz_decimals=instrument.sz_decimals)
    is_buy = position.side is PositionSide.SHORT
    action: dict[str, object] = {
        "type": "order",
        "orders": [
            {
                "a": instrument.asset_id,
                "b": is_buy,
                "p": price_wire,
                "s": size_wire,
                "r": True,
                "t": {"limit": {"tif": "Ioc"}},
                "c": checked_cloid,
            }
        ],
        "grouping": "na",
    }
    expires = _expiry(at_ms, ttl_ms)
    material = {
        "kind": RecoveryKind.REDUCE_ONLY_CLOSE.value,
        "network": selected_network.value,
        "account_id": checked_account,
        "main_account_address": main_address,
        "incident_id": selected_incident.incident_id,
        "position_snapshot_hash": snapshot.snapshot_hash,
        "position_snapshot_time_ms": snapshot.server_time_ms,
        "symbol": symbol,
        "asset_id": instrument.asset_id,
        "original_signed_position": canonical_decimal(position.signed_size),
        "close_size": canonical_decimal(requested),
        "price_bound": canonical_decimal(bound),
        "cloid": checked_cloid,
        "expires_at_ms": expires,
        "action": action,
    }
    return ReduceOnlyCloseAction(
        kind=RecoveryKind.REDUCE_ONLY_CLOSE,
        network=selected_network,
        account_id=checked_account,
        main_account_address=main_address,
        incident_id=selected_incident.incident_id,
        position_snapshot_hash=snapshot.snapshot_hash,
        symbol=symbol,
        asset_id=instrument.asset_id,
        original_signed_position=position.signed_size,
        close_size=requested,
        price_bound=bound,
        cloid=checked_cloid,
        expires_at_ms=expires,
        action=action,
        recovery_hash=_recovery_hash(material),
        position_snapshot_time_ms=snapshot.server_time_ms,
    )


def build_cancel_by_cloid(
    snapshot: HyperliquidAccountSnapshot,
    requests: Iterable[CancelRequest],
    *,
    owned_cloids: Iterable[str],
    incident: IncidentRecord,
    account_id: str,
    network: HyperliquidNetwork,
    at: datetime,
    ttl_ms: int = 10_000,
) -> CancelByCloidAction:
    """Build a bounded cancel batch without removing live downside protection.

    Ownership is necessary but not sufficient: every requested CLOID must be
    present in the fresh account snapshot and match the requested symbol.  If
    that symbol has an open position, only reduce-only non-stop orders may be
    cancelled.  In particular, an owned protective stop is never cancellable
    while it is responsible for an open position.
    """

    selected_network = _network(network)
    at_ms = _utc_ms(at, "at")
    _fresh_snapshot(snapshot, network=selected_network, at_ms=at_ms)
    selected_incident = _active_incident(incident)
    checked_account = _text(account_id, "account_id")
    main_address = _address(snapshot.main_account_address, "main_account_address")
    checked_requests = tuple(requests)
    if (
        not checked_requests
        or len(checked_requests) > _MAX_CANCELS
        or any(not isinstance(item, CancelRequest) for item in checked_requests)
    ):
        raise ValidationError("cancel requests must contain one to twenty typed values")
    owned = frozenset(_cloid(value, "owned CLOID") for value in owned_cloids)
    request_cloids = tuple(item.cloid for item in checked_requests)
    if len(set(request_cloids)) != len(request_cloids):
        raise ValidationError("cancel requests contain duplicate CLOIDs")
    if not set(request_cloids).issubset(owned):
        raise ValidationError("cancelByCloid may reference only owned CLOIDs")
    open_orders = snapshot.all_open_orders()
    for request in checked_requests:
        matches = tuple(order for order in open_orders if order.cloid == request.cloid)
        if len(matches) != 1:
            raise ValidationError(
                "cancelByCloid requires each CLOID exactly once in the fresh snapshot"
            )
        order = matches[0]
        if order.symbol != request.symbol:
            raise ValidationError("cancel request symbol differs from the live order")
        if snapshot.position(request.symbol) is not None and (
            order.is_protective_stop or not order.reduce_only
        ):
            raise ValidationError(
                "cannot cancel a protective stop or exposure-increasing order "
                "while its position is open"
            )
    asset_ids = tuple(
        snapshot.metadata.instrument(item.symbol).asset_id for item in checked_requests
    )
    action: dict[str, object] = {
        "type": "cancelByCloid",
        "cancels": [
            {"asset": asset_id, "cloid": item.cloid}
            for item, asset_id in zip(checked_requests, asset_ids)
        ],
    }
    expires = _expiry(at_ms, ttl_ms)
    material = {
        "kind": RecoveryKind.CANCEL_BY_CLOID.value,
        "network": selected_network.value,
        "account_id": checked_account,
        "main_account_address": main_address,
        "incident_id": selected_incident.incident_id,
        "account_snapshot_hash": snapshot.snapshot_hash,
        "account_snapshot_time_ms": snapshot.server_time_ms,
        "requests": [
            {"symbol": item.symbol, "asset_id": asset_id, "cloid": item.cloid}
            for item, asset_id in zip(checked_requests, asset_ids)
        ],
        "expires_at_ms": expires,
        "action": action,
    }
    return CancelByCloidAction(
        kind=RecoveryKind.CANCEL_BY_CLOID,
        network=selected_network,
        account_id=checked_account,
        main_account_address=main_address,
        incident_id=selected_incident.incident_id,
        account_snapshot_hash=snapshot.snapshot_hash,
        requests=checked_requests,
        asset_ids=asset_ids,
        expires_at_ms=expires,
        action=action,
        recovery_hash=_recovery_hash(material),
        account_snapshot_time_ms=snapshot.server_time_ms,
    )


def ambiguous_attempt_hash(attempt: AttemptRecord) -> str:
    if not isinstance(attempt, AttemptRecord):
        raise TypeError("attempt must be a persisted AttemptRecord")
    preflight_hash = (
        None
        if attempt.preflight_hash is None
        else _hash(attempt.preflight_hash, "attempt preflight_hash")
    )
    signed_evidence_hash = (
        None
        if attempt.signed_evidence_hash is None
        else _hash(attempt.signed_evidence_hash, "attempt signed_evidence_hash")
    )
    transport_evidence_hash = (
        None
        if attempt.transport_evidence_hash is None
        else _hash(
            attempt.transport_evidence_hash,
            "attempt transport_evidence_hash",
        )
    )
    return domain_hash(
        AMBIGUOUS_ATTEMPT_HASH_DOMAIN,
        {
            "attempt_id": _text(attempt.attempt_id, "attempt_id"),
            "command_id": _text(attempt.command_id, "command_id"),
            "worker_id": _text(attempt.worker_id, "worker_id"),
            "fencing_token": attempt.fencing_token,
            "preflight_hash": preflight_hash,
            "signed_evidence_hash": signed_evidence_hash,
            "transport_evidence_hash": transport_evidence_hash,
            "nonce": attempt.nonce,
            "action_hash": _hash(attempt.action_hash, "attempt action_hash"),
            "wire_hash": _hash(attempt.wire_hash, "attempt wire_hash"),
            "state": attempt.state,
            "response_hash": attempt.response_hash,
            "prepared_at": attempt.prepared_at,
            "updated_at": attempt.updated_at,
        },
    )


def build_noop_fence(
    attempt: AttemptRecord,
    *,
    incident: IncidentRecord,
    account_id: str,
    main_account_address: str,
    network: HyperliquidNetwork,
    at: datetime,
    ttl_ms: int = 10_000,
) -> NoopFenceAction:
    """Fence one durable unknown attempt using its exact original nonce."""

    if not isinstance(attempt, AttemptRecord):
        raise TypeError("attempt must be a persisted AttemptRecord")
    if attempt.state != "unknown" or attempt.response_hash is not None:
        raise ValidationError("noop fence requires a persisted unknown attempt")
    if (
        attempt.preflight_hash is None
        or attempt.signed_evidence_hash is None
        or attempt.transport_evidence_hash is None
    ):
        raise ValidationError("noop fence requires complete persisted ambiguity evidence")
    if type(attempt.nonce) is not int or attempt.nonce < 0:
        raise ValidationError("persisted unknown attempt nonce is invalid")
    selected_incident = _active_incident(incident)
    if selected_incident.command_id != attempt.command_id:
        raise ValidationError("incident and ambiguous attempt command IDs differ")
    selected_network = _network(network)
    at_ms = _utc_ms(at, "at")
    expires = _expiry(at_ms, ttl_ms)
    checked_account = _text(account_id, "account_id")
    main_address = _address(main_account_address, "main_account_address")
    source_hash = ambiguous_attempt_hash(attempt)
    action: dict[str, object] = {"type": "noop"}
    material = {
        "kind": RecoveryKind.NOOP_FENCE.value,
        "network": selected_network.value,
        "account_id": checked_account,
        "main_account_address": main_address,
        "incident_id": selected_incident.incident_id,
        "attempt_id": attempt.attempt_id,
        "command_id": attempt.command_id,
        "preflight_hash": attempt.preflight_hash,
        "signed_evidence_hash": attempt.signed_evidence_hash,
        "transport_evidence_hash": attempt.transport_evidence_hash,
        "original_nonce": attempt.nonce,
        "original_action_hash": attempt.action_hash,
        "original_wire_hash": attempt.wire_hash,
        "ambiguous_attempt_hash": source_hash,
        "expires_at_ms": expires,
        "action": action,
    }
    return NoopFenceAction(
        kind=RecoveryKind.NOOP_FENCE,
        network=selected_network,
        account_id=checked_account,
        main_account_address=main_address,
        incident_id=selected_incident.incident_id,
        attempt_id=attempt.attempt_id,
        command_id=attempt.command_id,
        preflight_hash=attempt.preflight_hash,
        signed_evidence_hash=attempt.signed_evidence_hash,
        transport_evidence_hash=attempt.transport_evidence_hash,
        original_nonce=attempt.nonce,
        original_action_hash=attempt.action_hash,
        original_wire_hash=attempt.wire_hash,
        ambiguous_attempt_hash=source_hash,
        expires_at_ms=expires,
        action=action,
        recovery_hash=_recovery_hash(material),
    )


__all__ = (
    "AMBIGUOUS_ATTEMPT_HASH_DOMAIN",
    "RECOVERY_ACTION_HASH_DOMAIN",
    "RECOVERY_CLOSE_CLOID_DOMAIN",
    "CancelByCloidAction",
    "CancelRequest",
    "NoopFenceAction",
    "RecoveryAction",
    "RecoveryKind",
    "ReduceOnlyCloseAction",
    "ambiguous_attempt_hash",
    "build_cancel_by_cloid",
    "build_noop_fence",
    "build_reduce_only_close",
    "derive_recovery_close_cloid",
    "recovery_action_from_material",
    "recovery_action_material",
)
