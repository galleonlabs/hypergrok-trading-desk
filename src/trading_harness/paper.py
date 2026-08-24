"""Deterministic local-paper OMS and protection state machine.

This module consumes a validated :class:`~trading_harness.planning.ProtectedTradePlan`
and simulates its lifecycle from caller-supplied, point-in-time observations.
It has no network transport, signer, credential path, exchange adapter, or
testnet/mainnet write capability.  A result is always labelled ``local_paper``.

The simulated grouping follows the conservative ``normalTpsl`` contract used
by this project: stop and take-profit children may activate only after the
entry is fully filled.  A partial IOC fill is immediately treated as
under-protected exposure and enters the emergency reduce-only flatten path.
Missing, rejected, contradictory, or undersized protection halts new risk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from enum import Enum
import re
from typing import Any

from .canonical import canonical_data, domain_hash, validate_decimal_bounds
from .domain import Side
from .planning import GroupingPolicy, ProtectedTradePlan


PAPER_COST_HASH_DOMAIN = "trading-harness/paper-cost-model/v1"
PAPER_EVENT_HASH_DOMAIN = "trading-harness/paper-event/v1"
PAPER_CHAIN_HASH_DOMAIN = "trading-harness/paper-event-chain/v1"
PAPER_SNAPSHOT_HASH_DOMAIN = "trading-harness/paper-snapshot/v1"
BASIS_POINTS = Decimal("10000")
ZERO = Decimal("0")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CALCULATION_CONTEXT = Context(
    prec=64,
    rounding=ROUND_HALF_EVEN,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)


class PaperStateError(ValueError):
    """A paper transition or observation violates the local OMS contract."""


class PaperState(str, Enum):
    FLAT = "flat"
    PENDING = "pending"
    UNPROTECTED = "unprotected"
    PROTECTED = "protected"
    UNDERPROTECTED = "underprotected"
    HALTED = "halted"


class EntryFillStatus(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    UNFILLED = "unfilled"


class LegStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MISSING = "missing"


class PaperEventType(str, Enum):
    PLAN_ACCEPTED = "plan_accepted"
    ENTRY_FULL = "entry_full"
    ENTRY_PARTIAL = "entry_partial"
    ENTRY_UNFILLED = "entry_unfilled"
    PROTECTION_ACTIVATED = "protection_activated"
    PROTECTION_CONFIRMED = "protection_confirmed"
    PROTECTION_UNDER_SIZED = "protection_under_sized"
    PROTECTION_REJECTED = "protection_rejected"
    PROTECTION_TIMEOUT = "protection_timeout"
    MARKET_OBSERVED = "market_observed"
    STOP_FILLED = "stop_filled"
    TARGET_FILLED = "target_filled"
    STOP_UNFILLED_HALT = "stop_unfilled_halt"
    TARGET_UNFILLED_HALT = "target_unfilled_halt"
    EMERGENCY_FLATTEN_FULL = "emergency_flatten_full"
    EMERGENCY_FLATTEN_PARTIAL = "emergency_flatten_partial"
    EMERGENCY_FLATTEN_UNFILLED = "emergency_flatten_unfilled"
    HALT_ACKNOWLEDGED = "halt_acknowledged"


def _text(value: object, field: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be a bounded, non-empty, trimmed string")
    return value


def _hash(value: object, field: str) -> str:
    parsed = _text(value, field, 64)
    if not _SHA256_RE.fullmatch(parsed):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return parsed


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal, int, or exact string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        validate_decimal_bounds(parsed, field=field)
    except (DecimalException, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a bounded finite decimal") from error
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be greater than zero")
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.add(left, right)


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.subtract(left, right)


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.multiply(left, right)


def _divide(left: Decimal, right: Decimal) -> Decimal:
    if right == 0:
        raise ZeroDivisionError("paper decimal denominator is zero")
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.divide(left, right)


def _bps_amount(value: Decimal, bps: Decimal) -> Decimal:
    return _divide(_multiply(value, bps), BASIS_POINTS)


@dataclass(frozen=True, slots=True)
class PaperCostModel:
    model_id: str
    version: str
    fee_bps_per_fill: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal
    emergency_slippage_bps: Decimal
    protection_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        for field in ("model_id", "version"):
            object.__setattr__(self, field, _text(getattr(self, field), field, 64))
        for field in (
            "fee_bps_per_fill",
            "entry_slippage_bps",
            "exit_slippage_bps",
            "emergency_slippage_bps",
        ):
            parsed = _decimal(getattr(self, field), field, nonnegative=True)
            if parsed > Decimal("2500"):
                raise ValueError(f"{field} exceeds the local paper bound")
            object.__setattr__(self, field, parsed)
        if (
            type(self.protection_timeout_seconds) is not int
            or self.protection_timeout_seconds <= 0
        ):
            raise ValueError("protection_timeout_seconds must be a positive integer")

    @property
    def model_hash(self) -> str:
        return domain_hash(PAPER_COST_HASH_DOMAIN, self)


@dataclass(frozen=True, slots=True)
class PaperBookObservation:
    observation_id: str
    observed_at: datetime
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        object.__setattr__(self, "observed_at", _instant(self.observed_at, "observed_at"))
        for field in ("bid_price", "ask_price"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field, positive=True))
        for field in ("bid_size", "ask_size"):
            object.__setattr__(
                self, field, _decimal(getattr(self, field), field, nonnegative=True)
            )
        if self.bid_price > self.ask_price:
            raise ValueError("paper book bid cannot exceed ask")
        object.__setattr__(self, "source_hash", _hash(self.source_hash, "source_hash"))


@dataclass(frozen=True, slots=True)
class PaperCandleObservation:
    observation_id: str
    open_time: datetime
    close_time: datetime
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source_hash: str
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        opened = _instant(self.open_time, "open_time")
        closed = _instant(self.close_time, "close_time")
        observed = _instant(self.observed_at, "observed_at")
        object.__setattr__(self, "open_time", opened)
        object.__setattr__(self, "close_time", closed)
        object.__setattr__(self, "observed_at", observed)
        if closed <= opened or observed < closed:
            raise ValueError("paper candle must be complete before observation")
        if type(self.complete) is not bool or not self.complete:
            raise ValueError("paper candle observation must be complete")
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field, positive=True))
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("paper candle high is invalid")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("paper candle low is invalid")
        object.__setattr__(self, "source_hash", _hash(self.source_hash, "source_hash"))


@dataclass(frozen=True, slots=True)
class PaperProtectionObservation:
    observation_id: str
    observed_at: datetime
    stop_status: LegStatus
    take_profit_status: LegStatus
    stop_quantity: Decimal
    take_profit_quantity: Decimal
    source_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        object.__setattr__(self, "observed_at", _instant(self.observed_at, "observed_at"))
        for field in ("stop_status", "take_profit_status"):
            value = getattr(self, field)
            if not isinstance(value, LegStatus):
                try:
                    object.__setattr__(self, field, LegStatus(value))
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{field} is invalid") from error
        for field in ("stop_quantity", "take_profit_quantity"):
            object.__setattr__(
                self, field, _decimal(getattr(self, field), field, nonnegative=True)
            )
        pairs = (
            (self.stop_status, self.stop_quantity, "stop"),
            (self.take_profit_status, self.take_profit_quantity, "take_profit"),
        )
        for status, quantity, name in pairs:
            if (status is LegStatus.ACCEPTED) != (quantity > 0):
                raise ValueError(f"{name} accepted status and quantity disagree")
        object.__setattr__(self, "source_hash", _hash(self.source_hash, "source_hash"))


@dataclass(frozen=True, slots=True)
class PaperEvent:
    event_id: str
    event_type: PaperEventType
    observed_at: datetime
    reason: str
    plan_hash: str | None = None
    source_hash: str | None = None
    quantity: Decimal = ZERO
    secondary_quantity: Decimal = ZERO
    price: Decimal | None = None
    fee: Decimal = ZERO
    realized_net_pnl_delta: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        if not isinstance(self.event_type, PaperEventType):
            try:
                object.__setattr__(self, "event_type", PaperEventType(self.event_type))
            except (TypeError, ValueError) as error:
                raise ValueError("event_type is invalid") from error
        object.__setattr__(self, "observed_at", _instant(self.observed_at, "observed_at"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if self.plan_hash is not None:
            object.__setattr__(self, "plan_hash", _hash(self.plan_hash, "plan_hash"))
        if self.source_hash is not None:
            object.__setattr__(self, "source_hash", _hash(self.source_hash, "source_hash"))
        for field in ("quantity", "secondary_quantity", "fee"):
            object.__setattr__(
                self, field, _decimal(getattr(self, field), field, nonnegative=True)
            )
        if self.price is not None:
            object.__setattr__(self, "price", _decimal(self.price, "price", positive=True))
        object.__setattr__(
            self,
            "realized_net_pnl_delta",
            _decimal(self.realized_net_pnl_delta, "realized_net_pnl_delta"),
        )

    @property
    def event_hash(self) -> str:
        return domain_hash(PAPER_EVENT_HASH_DOMAIN, self)


def _genesis_hash(account_id: str, instrument: str, cost_model_hash: str) -> str:
    return domain_hash(
        PAPER_CHAIN_HASH_DOMAIN,
        {
            "account_id": account_id,
            "instrument": instrument,
            "cost_model_hash": cost_model_hash,
            "state": "empty",
        },
    )


def _event_chain_hash(
    account_id: str,
    instrument: str,
    cost_model_hash: str,
    events: tuple[PaperEvent, ...],
) -> str:
    previous = _genesis_hash(account_id, instrument, cost_model_hash)
    for sequence, event in enumerate(events):
        previous = domain_hash(
            PAPER_CHAIN_HASH_DOMAIN,
            {
                "sequence": sequence,
                "previous": previous,
                "event_hash": event.event_hash,
            },
        )
    return previous


@dataclass(frozen=True, slots=True)
class PaperOMS:
    """Immutable, restartable local-paper account/instrument state."""

    account_id: str
    instrument: str
    cost_model: PaperCostModel
    state: PaperState
    halted: bool
    plan: ProtectedTradePlan | None
    position_side: Side | None
    position_quantity: Decimal
    entry_fill_quantity: Decimal
    average_entry_price: Decimal | None
    active_stop_quantity: Decimal
    active_take_profit_quantity: Decimal
    open_entry_fee: Decimal
    fees_paid: Decimal
    realized_net_pnl: Decimal
    last_observed_at: datetime | None
    events: tuple[PaperEvent, ...]
    chain_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument", 64))
        if not isinstance(self.cost_model, PaperCostModel):
            raise TypeError("cost_model must be PaperCostModel")
        if not isinstance(self.state, PaperState):
            try:
                object.__setattr__(self, "state", PaperState(self.state))
            except (TypeError, ValueError) as error:
                raise ValueError("state is invalid") from error
        if type(self.halted) is not bool:
            raise TypeError("halted must be bool")
        if self.plan is not None and not isinstance(self.plan, ProtectedTradePlan):
            raise TypeError("plan must be ProtectedTradePlan or None")
        if self.position_side is not None and not isinstance(self.position_side, Side):
            try:
                object.__setattr__(self, "position_side", Side(self.position_side))
            except (TypeError, ValueError) as error:
                raise ValueError("position_side is invalid") from error
        for field in (
            "position_quantity",
            "entry_fill_quantity",
            "active_stop_quantity",
            "active_take_profit_quantity",
            "open_entry_fee",
            "fees_paid",
        ):
            object.__setattr__(
                self, field, _decimal(getattr(self, field), field, nonnegative=True)
            )
        if self.average_entry_price is not None:
            object.__setattr__(
                self,
                "average_entry_price",
                _decimal(self.average_entry_price, "average_entry_price", positive=True),
            )
        object.__setattr__(
            self,
            "realized_net_pnl",
            _decimal(self.realized_net_pnl, "realized_net_pnl"),
        )
        if self.last_observed_at is not None:
            object.__setattr__(
                self,
                "last_observed_at",
                _instant(self.last_observed_at, "last_observed_at"),
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be integer 1")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, PaperEvent) for event in self.events
        ):
            raise TypeError("events must be a tuple of PaperEvent values")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise PaperStateError("event_id values must be unique")
        expected_chain = _event_chain_hash(
            self.account_id,
            self.instrument,
            self.cost_model.model_hash,
            self.events,
        )
        if _hash(self.chain_hash, "chain_hash") != expected_chain:
            raise PaperStateError("paper event chain does not match its events")
        expected_last = self.events[-1].observed_at if self.events else None
        if self.last_observed_at != expected_last:
            raise PaperStateError("last_observed_at does not match the event stream")
        for left, right in zip(self.events, self.events[1:]):
            if right.observed_at < left.observed_at:
                raise PaperStateError("paper events are out of observed-time order")

        has_position = self.position_quantity > 0
        if has_position != (self.position_side is not None):
            raise PaperStateError("position side and quantity disagree")
        if has_position != (self.average_entry_price is not None):
            raise PaperStateError("position price and quantity disagree")
        if has_position:
            if self.plan is None or self.entry_fill_quantity < self.position_quantity:
                raise PaperStateError("open paper position lacks its source plan/fill")
        elif any(
            value != 0
            for value in (
                self.entry_fill_quantity,
                self.active_stop_quantity,
                self.active_take_profit_quantity,
                self.open_entry_fee,
            )
        ):
            raise PaperStateError("flat quantity cannot retain open-position economics")
        if self.active_stop_quantity > self.position_quantity or self.active_take_profit_quantity > self.position_quantity:
            raise PaperStateError("paper protection exceeds live position quantity")
        if self.plan is not None and (
            self.plan.entry.account_id != self.account_id
            or self.plan.entry.instrument != self.instrument
        ):
            raise PaperStateError("paper plan targets a different account or instrument")

        if self.state is PaperState.FLAT:
            if has_position or self.plan is not None or self.halted:
                raise PaperStateError("flat state cannot retain plan, position, or halt")
        elif self.state is PaperState.PENDING:
            if has_position or self.plan is None or self.halted:
                raise PaperStateError("pending state requires only an active plan")
        elif self.state is PaperState.UNPROTECTED:
            if not has_position or self.halted or self.active_stop_quantity or self.active_take_profit_quantity:
                raise PaperStateError("unprotected state invariants failed")
        elif self.state is PaperState.PROTECTED:
            if (
                not has_position
                or self.halted
                or self.active_stop_quantity != self.position_quantity
                or self.active_take_profit_quantity != self.position_quantity
            ):
                raise PaperStateError("protected state requires exact full bracket coverage")
        elif self.state is PaperState.UNDERPROTECTED:
            if not has_position or not self.halted or self.active_stop_quantity >= self.position_quantity:
                raise PaperStateError("underprotected state invariants failed")
        elif not self.halted:
            raise PaperStateError("halted state requires halted=True")
        if self.state is PaperState.HALTED and has_position and self.plan is None:
            raise PaperStateError("halted exposure must retain its source plan")

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        instrument: str,
        cost_model: PaperCostModel,
    ) -> "PaperOMS":
        checked_account = _text(account_id, "account_id")
        checked_instrument = _text(instrument, "instrument", 64)
        if not isinstance(cost_model, PaperCostModel):
            raise TypeError("cost_model must be PaperCostModel")
        return cls(
            account_id=checked_account,
            instrument=checked_instrument,
            cost_model=cost_model,
            state=PaperState.FLAT,
            halted=False,
            plan=None,
            position_side=None,
            position_quantity=ZERO,
            entry_fill_quantity=ZERO,
            average_entry_price=None,
            active_stop_quantity=ZERO,
            active_take_profit_quantity=ZERO,
            open_entry_fee=ZERO,
            fees_paid=ZERO,
            realized_net_pnl=ZERO,
            last_observed_at=None,
            events=(),
            chain_hash=_genesis_hash(
                checked_account, checked_instrument, cost_model.model_hash
            ),
        )

    @property
    def snapshot_hash(self) -> str:
        return domain_hash(PAPER_SNAPSHOT_HASH_DOMAIN, self)

    @property
    def emergency_flatten_required(self) -> bool:
        return self.position_quantity > 0 and (
            self.halted
            or self.state in {PaperState.UNPROTECTED, PaperState.UNDERPROTECTED}
        )

    @property
    def entry_fill_status(self) -> EntryFillStatus | None:
        for event in reversed(self.events):
            if event.event_type is PaperEventType.ENTRY_FULL:
                return EntryFillStatus.FULL
            if event.event_type is PaperEventType.ENTRY_PARTIAL:
                return EntryFillStatus.PARTIAL
            if event.event_type is PaperEventType.ENTRY_UNFILLED:
                return EntryFillStatus.UNFILLED
        return None

    @classmethod
    def restore(cls, checkpoint: "PaperOMS", *, expected_hash: str) -> "PaperOMS":
        if not isinstance(checkpoint, cls):
            raise TypeError("checkpoint must be PaperOMS")
        if checkpoint.snapshot_hash != _hash(expected_hash, "expected_hash"):
            raise PaperStateError("paper checkpoint hash mismatch")
        # Reconstruct through the public dataclass boundary to repeat all chain
        # and cross-field checks after deserialization.
        return cls(**{field: getattr(checkpoint, field) for field in checkpoint.__dataclass_fields__})

    def _append(self, event: PaperEvent, **changes: Any) -> "PaperOMS":
        if event.event_id in {existing.event_id for existing in self.events}:
            raise PaperStateError("event_id is already present")
        if event.source_hash is not None and event.source_hash in {
            existing.source_hash
            for existing in self.events
            if existing.source_hash is not None
        }:
            raise PaperStateError("source observation was already consumed")
        if self.last_observed_at is not None and event.observed_at < self.last_observed_at:
            raise PaperStateError("paper observation predates current state")
        events = self.events + (event,)
        chain_hash = domain_hash(
            PAPER_CHAIN_HASH_DOMAIN,
            {
                "sequence": len(self.events),
                "previous": self.chain_hash,
                "event_hash": event.event_hash,
            },
        )
        return replace(
            self,
            events=events,
            chain_hash=chain_hash,
            last_observed_at=event.observed_at,
            **changes,
        )

    def submit_plan(
        self,
        plan: ProtectedTradePlan,
        *,
        at: datetime,
        event_id: str,
    ) -> "PaperOMS":
        if self.state is not PaperState.FLAT or self.halted or self.position_quantity:
            raise PaperStateError("new paper risk requires a non-halted flat state")
        if not isinstance(plan, ProtectedTradePlan):
            raise TypeError("plan must be ProtectedTradePlan")
        checked_at = _instant(at, "at")
        if plan.grouping is not GroupingPolicy.NORMAL_TPSL:
            raise PaperStateError("paper OMS accepts only normalTpsl plans")
        if plan.entry.account_id != self.account_id or plan.entry.instrument != self.instrument:
            raise PaperStateError("plan targets a different account or instrument")
        if checked_at >= plan.entry.expires_at:
            raise PaperStateError("paper plan is already expired")
        event = PaperEvent(
            event_id=event_id,
            event_type=PaperEventType.PLAN_ACCEPTED,
            observed_at=checked_at,
            reason="local_paper_plan_accepted_no_venue_write",
            plan_hash=plan.plan_hash,
        )
        return self._append(event, state=PaperState.PENDING, plan=plan)

    def observe_entry(
        self, observation: PaperBookObservation, *, event_id: str
    ) -> "PaperOMS":
        if self.state is not PaperState.PENDING or self.plan is None:
            raise PaperStateError("paper entry observation requires a pending plan")
        if not isinstance(observation, PaperBookObservation):
            raise TypeError("observation must be PaperBookObservation")
        entry = self.plan.entry
        bound = entry.price_bound
        if bound is None:  # ProtectedTradePlan already guarantees this.
            raise PaperStateError("protected entry has no price bound")

        reason = "bounded_ioc_unfilled"
        fill_price: Decimal | None = None
        available = ZERO
        if observation.observed_at >= entry.expires_at:
            reason = "entry_expired_before_observation"
        elif self.cost_model.entry_slippage_bps > entry.max_slippage_bps:
            reason = "paper_entry_slippage_exceeds_plan"
        elif entry.side is Side.BUY:
            candidate = _add(
                observation.ask_price,
                _bps_amount(observation.ask_price, self.cost_model.entry_slippage_bps),
            )
            if candidate <= bound:
                fill_price = candidate
                available = observation.ask_size
            else:
                reason = "buy_ioc_price_bound_not_crossable"
        else:
            candidate = _subtract(
                observation.bid_price,
                _bps_amount(observation.bid_price, self.cost_model.entry_slippage_bps),
            )
            if candidate >= bound:
                fill_price = candidate
                available = observation.bid_size
            else:
                reason = "sell_ioc_price_bound_not_crossable"

        fill_quantity = min(entry.quantity, available) if fill_price is not None else ZERO
        if fill_quantity <= 0:
            event = PaperEvent(
                event_id=event_id,
                event_type=PaperEventType.ENTRY_UNFILLED,
                observed_at=observation.observed_at,
                reason=reason,
                plan_hash=self.plan.plan_hash,
                source_hash=observation.source_hash,
            )
            return self._append(event, state=PaperState.FLAT, plan=None)

        fee = _bps_amount(
            _multiply(cast_price := fill_price, fill_quantity),
            self.cost_model.fee_bps_per_fill,
        )
        full = fill_quantity == entry.quantity
        event = PaperEvent(
            event_id=event_id,
            event_type=(PaperEventType.ENTRY_FULL if full else PaperEventType.ENTRY_PARTIAL),
            observed_at=observation.observed_at,
            reason=(
                "bounded_ioc_fully_filled_children_not_yet_active"
                if full
                else "bounded_ioc_partial_fill_requires_emergency_flatten"
            ),
            plan_hash=self.plan.plan_hash,
            source_hash=observation.source_hash,
            quantity=fill_quantity,
            price=cast_price,
            fee=fee,
        )
        return self._append(
            event,
            state=(PaperState.UNPROTECTED if full else PaperState.UNDERPROTECTED),
            halted=not full,
            position_side=entry.side,
            position_quantity=fill_quantity,
            entry_fill_quantity=fill_quantity,
            average_entry_price=cast_price,
            open_entry_fee=fee,
            fees_paid=_add(self.fees_paid, fee),
        )

    def reconcile_protection(
        self,
        observation: PaperProtectionObservation,
        *,
        event_id: str,
    ) -> "PaperOMS":
        if self.state not in {PaperState.UNPROTECTED, PaperState.PROTECTED} or self.plan is None:
            raise PaperStateError("protection reconciliation requires full entry exposure")
        if not isinstance(observation, PaperProtectionObservation):
            raise TypeError("observation must be PaperProtectionObservation")
        position = self.position_quantity
        stop_reported = observation.stop_quantity
        target_reported = observation.take_profit_quantity
        stop_active = min(stop_reported, position)
        target_active = min(target_reported, position)
        stop_exact = (
            observation.stop_status is LegStatus.ACCEPTED and stop_reported == position
        )
        target_exact = (
            observation.take_profit_status is LegStatus.ACCEPTED
            and target_reported == position
        )
        if stop_exact and target_exact:
            kind = (
                PaperEventType.PROTECTION_ACTIVATED
                if self.state is PaperState.UNPROTECTED
                else PaperEventType.PROTECTION_CONFIRMED
            )
            event = PaperEvent(
                event_id=event_id,
                event_type=kind,
                observed_at=observation.observed_at,
                reason="normalTpsl_children_confirmed_for_full_position",
                plan_hash=self.plan.plan_hash,
                source_hash=observation.source_hash,
                quantity=stop_reported,
                secondary_quantity=target_reported,
            )
            return self._append(
                event,
                state=PaperState.PROTECTED,
                halted=False,
                active_stop_quantity=position,
                active_take_profit_quantity=position,
            )

        stop_under = (
            observation.stop_status is LegStatus.ACCEPTED
            and ZERO < stop_reported < position
        )
        kind = (
            PaperEventType.PROTECTION_UNDER_SIZED
            if stop_under
            else PaperEventType.PROTECTION_REJECTED
        )
        reason = (
            "stop_quantity_under_covers_position_emergency_flatten_required"
            if stop_under
            else "normalTpsl_child_missing_rejected_or_contradictory"
        )
        state = PaperState.UNDERPROTECTED if stop_under else PaperState.HALTED
        event = PaperEvent(
            event_id=event_id,
            event_type=kind,
            observed_at=observation.observed_at,
            reason=reason,
            plan_hash=self.plan.plan_hash,
            source_hash=observation.source_hash,
            quantity=stop_reported,
            secondary_quantity=target_reported,
        )
        return self._append(
            event,
            state=state,
            halted=True,
            active_stop_quantity=stop_active,
            active_take_profit_quantity=target_active,
        )

    def protection_timeout(self, *, at: datetime, event_id: str) -> "PaperOMS":
        if self.state is not PaperState.UNPROTECTED or self.plan is None:
            raise PaperStateError("protection timeout requires unprotected full exposure")
        checked_at = _instant(at, "at")
        if self.last_observed_at is None or checked_at < self.last_observed_at + timedelta(
            seconds=self.cost_model.protection_timeout_seconds
        ):
            raise PaperStateError("protection timeout deadline has not elapsed")
        event = PaperEvent(
            event_id=event_id,
            event_type=PaperEventType.PROTECTION_TIMEOUT,
            observed_at=checked_at,
            reason="mandatory_stop_not_confirmed_before_deadline",
            plan_hash=self.plan.plan_hash,
        )
        return self._append(event, state=PaperState.HALTED, halted=True)

    def observe_protected_candle(
        self,
        observation: PaperCandleObservation,
        *,
        event_id: str,
    ) -> "PaperOMS":
        if self.state is not PaperState.PROTECTED or self.plan is None:
            raise PaperStateError("market lifecycle observation requires protected state")
        if not isinstance(observation, PaperCandleObservation):
            raise TypeError("observation must be PaperCandleObservation")
        if self.last_observed_at is not None and observation.open_time < self.last_observed_at:
            raise PaperStateError("candle begins before protection was observable")
        stop = self.plan.protective_stop
        target = self.plan.take_profit
        stop_trigger = stop.stop_price
        target_trigger = target.stop_price
        if stop_trigger is None or target_trigger is None:
            raise PaperStateError("protected plan triggers disappeared")

        if self.position_side is Side.BUY:
            if observation.open <= stop_trigger:
                reference, kind, reason = (
                    observation.open,
                    PaperEventType.STOP_FILLED,
                    "long_gap_through_stop",
                )
            elif observation.open >= target_trigger:
                reference, kind, reason = (
                    target_trigger,
                    PaperEventType.TARGET_FILLED,
                    "long_gap_above_target_filled_at_registered_target",
                )
            else:
                stop_touched = observation.low <= stop_trigger
                target_touched = observation.high >= target_trigger
                if stop_touched:
                    reference, kind, reason = (
                        stop_trigger,
                        PaperEventType.STOP_FILLED,
                        (
                            "stop_first_when_stop_and_target_share_completed_bar"
                            if target_touched
                            else "long_stop_touched"
                        ),
                    )
                elif target_touched:
                    reference, kind, reason = (
                        target_trigger,
                        PaperEventType.TARGET_FILLED,
                        "long_target_touched",
                    )
                else:
                    return self._append(
                        PaperEvent(
                            event_id=event_id,
                            event_type=PaperEventType.MARKET_OBSERVED,
                            observed_at=observation.observed_at,
                            reason="completed_candle_touched_neither_bracket_leg",
                            plan_hash=self.plan.plan_hash,
                            source_hash=observation.source_hash,
                        )
                    )
        else:
            if observation.open >= stop_trigger:
                reference, kind, reason = (
                    observation.open,
                    PaperEventType.STOP_FILLED,
                    "short_gap_through_stop",
                )
            elif observation.open <= target_trigger:
                reference, kind, reason = (
                    target_trigger,
                    PaperEventType.TARGET_FILLED,
                    "short_gap_below_target_filled_at_registered_target",
                )
            else:
                stop_touched = observation.high >= stop_trigger
                target_touched = observation.low <= target_trigger
                if stop_touched:
                    reference, kind, reason = (
                        stop_trigger,
                        PaperEventType.STOP_FILLED,
                        (
                            "stop_first_when_stop_and_target_share_completed_bar"
                            if target_touched
                            else "short_stop_touched"
                        ),
                    )
                elif target_touched:
                    reference, kind, reason = (
                        target_trigger,
                        PaperEventType.TARGET_FILLED,
                        "short_target_touched",
                    )
                else:
                    return self._append(
                        PaperEvent(
                            event_id=event_id,
                            event_type=PaperEventType.MARKET_OBSERVED,
                            observed_at=observation.observed_at,
                            reason="completed_candle_touched_neither_bracket_leg",
                            plan_hash=self.plan.plan_hash,
                            source_hash=observation.source_hash,
                        )
                    )

        if self.cost_model.exit_slippage_bps > (
            stop.max_slippage_bps if kind is PaperEventType.STOP_FILLED else target.max_slippage_bps
        ):
            return self._halt_failed_exit(
                observation,
                event_id=event_id,
                kind=(
                    PaperEventType.STOP_UNFILLED_HALT
                    if kind is PaperEventType.STOP_FILLED
                    else PaperEventType.TARGET_UNFILLED_HALT
                ),
                reason="paper_exit_slippage_exceeds_plan",
            )
        fill_price = self._adverse_exit(reference, self.cost_model.exit_slippage_bps)
        bound = stop.price_bound if kind is PaperEventType.STOP_FILLED else target.price_bound
        if bound is None or not self._exit_within_bound(fill_price, bound):
            return self._halt_failed_exit(
                observation,
                event_id=event_id,
                kind=(
                    PaperEventType.STOP_UNFILLED_HALT
                    if kind is PaperEventType.STOP_FILLED
                    else PaperEventType.TARGET_UNFILLED_HALT
                ),
                reason="triggered_paper_exit_could_not_fill_within_bound",
            )
        return self._close_position(
            event_id=event_id,
            event_type=kind,
            at=observation.observed_at,
            source_hash=observation.source_hash,
            price=fill_price,
            quantity=self.position_quantity,
            reason=reason,
            final_state=PaperState.FLAT,
            remain_halted=False,
        )

    def _halt_failed_exit(
        self,
        observation: PaperCandleObservation,
        *,
        event_id: str,
        kind: PaperEventType,
        reason: str,
    ) -> "PaperOMS":
        if self.plan is None:
            raise PaperStateError("failed paper exit lacks plan")
        event = PaperEvent(
            event_id=event_id,
            event_type=kind,
            observed_at=observation.observed_at,
            reason=reason,
            plan_hash=self.plan.plan_hash,
            source_hash=observation.source_hash,
        )
        return self._append(
            event,
            state=PaperState.HALTED,
            halted=True,
            active_stop_quantity=ZERO,
            active_take_profit_quantity=ZERO,
        )

    def _adverse_exit(self, reference: Decimal, slippage_bps: Decimal) -> Decimal:
        slip = _bps_amount(reference, slippage_bps)
        return (
            _subtract(reference, slip)
            if self.position_side is Side.BUY
            else _add(reference, slip)
        )

    def _exit_within_bound(self, price: Decimal, bound: Decimal) -> bool:
        return price >= bound if self.position_side is Side.BUY else price <= bound

    def emergency_flatten(
        self,
        observation: PaperBookObservation,
        *,
        event_id: str,
    ) -> "PaperOMS":
        if not self.emergency_flatten_required or self.plan is None:
            raise PaperStateError("emergency flatten requires unsafe live exposure")
        if not isinstance(observation, PaperBookObservation):
            raise TypeError("observation must be PaperBookObservation")
        if self.position_side is Side.BUY:
            reference = observation.bid_price
            available = observation.bid_size
        else:
            reference = observation.ask_price
            available = observation.ask_size
        quantity = min(self.position_quantity, available)
        if quantity <= 0:
            event = PaperEvent(
                event_id=event_id,
                event_type=PaperEventType.EMERGENCY_FLATTEN_UNFILLED,
                observed_at=observation.observed_at,
                reason="no_visible_liquidity_for_emergency_reduce_only_flatten",
                plan_hash=self.plan.plan_hash,
                source_hash=observation.source_hash,
            )
            return self._append(event, state=PaperState.HALTED, halted=True)
        price = self._adverse_exit(reference, self.cost_model.emergency_slippage_bps)
        if price <= 0:
            raise PaperStateError("emergency paper fill price is non-positive")
        full = quantity == self.position_quantity
        return self._close_position(
            event_id=event_id,
            event_type=(
                PaperEventType.EMERGENCY_FLATTEN_FULL
                if full
                else PaperEventType.EMERGENCY_FLATTEN_PARTIAL
            ),
            at=observation.observed_at,
            source_hash=observation.source_hash,
            price=price,
            quantity=quantity,
            reason=(
                "emergency_reduce_only_paper_flatten_complete"
                if full
                else "emergency_reduce_only_paper_flatten_partial"
            ),
            final_state=(PaperState.HALTED if full else PaperState.UNDERPROTECTED),
            remain_halted=True,
        )

    def _close_position(
        self,
        *,
        event_id: str,
        event_type: PaperEventType,
        at: datetime,
        source_hash: str,
        price: Decimal,
        quantity: Decimal,
        reason: str,
        final_state: PaperState,
        remain_halted: bool,
    ) -> "PaperOMS":
        if self.average_entry_price is None or self.position_side is None or self.plan is None:
            raise PaperStateError("paper close lacks open-position economics")
        if quantity <= 0 or quantity > self.position_quantity:
            raise PaperStateError("paper close quantity is invalid")
        position_before = self.position_quantity
        entry_fee_share = _multiply(
            self.open_entry_fee, _divide(quantity, position_before)
        )
        exit_fee = _bps_amount(
            _multiply(price, quantity), self.cost_model.fee_bps_per_fill
        )
        gross = _multiply(
            (
                _subtract(price, self.average_entry_price)
                if self.position_side is Side.BUY
                else _subtract(self.average_entry_price, price)
            ),
            quantity,
        )
        realized_delta = _subtract(
            _subtract(gross, entry_fee_share), exit_fee
        )
        remaining = _subtract(position_before, quantity)
        event = PaperEvent(
            event_id=event_id,
            event_type=event_type,
            observed_at=at,
            reason=reason,
            plan_hash=self.plan.plan_hash,
            source_hash=source_hash,
            quantity=quantity,
            price=price,
            fee=exit_fee,
            realized_net_pnl_delta=realized_delta,
        )
        common = {
            "fees_paid": _add(self.fees_paid, exit_fee),
            "realized_net_pnl": _add(self.realized_net_pnl, realized_delta),
        }
        if remaining == 0:
            return self._append(
                event,
                state=final_state,
                halted=remain_halted,
                plan=None,
                position_side=None,
                position_quantity=ZERO,
                entry_fill_quantity=ZERO,
                average_entry_price=None,
                active_stop_quantity=ZERO,
                active_take_profit_quantity=ZERO,
                open_entry_fee=ZERO,
                **common,
            )
        remaining_entry_fee = _subtract(self.open_entry_fee, entry_fee_share)
        remaining_stop = min(self.active_stop_quantity, remaining)
        remaining_target = min(self.active_take_profit_quantity, remaining)
        remaining_state = (
            PaperState.UNDERPROTECTED
            if remaining_stop < remaining
            else PaperState.HALTED
        )
        return self._append(
            event,
            state=remaining_state,
            halted=True,
            position_quantity=remaining,
            active_stop_quantity=remaining_stop,
            active_take_profit_quantity=remaining_target,
            open_entry_fee=remaining_entry_fee,
            **common,
        )

    def acknowledge_halt(
        self,
        *,
        at: datetime,
        event_id: str,
        review_hash: str,
    ) -> "PaperOMS":
        if self.state is not PaperState.HALTED or self.position_quantity != 0:
            raise PaperStateError("halt can be acknowledged only after paper account is flat")
        checked_at = _instant(at, "at")
        checked_review = _hash(review_hash, "review_hash")
        event = PaperEvent(
            event_id=event_id,
            event_type=PaperEventType.HALT_ACKNOWLEDGED,
            observed_at=checked_at,
            reason="paper_halt_acknowledged_after_flat_review",
            source_hash=checked_review,
        )
        return self._append(event, state=PaperState.FLAT, halted=False)

    def to_dict(self) -> dict[str, Any]:
        value = canonical_data(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("canonical paper snapshot must be an object")
        return {
            **value,
            "snapshot_hash": self.snapshot_hash,
            "mode": "local_paper",
            "venue_execution": False,
            "testnet_execution": False,
            "mainnet_execution": False,
        }


__all__ = (
    "EntryFillStatus",
    "LegStatus",
    "PaperBookObservation",
    "PaperCandleObservation",
    "PaperCostModel",
    "PaperEvent",
    "PaperEventType",
    "PaperOMS",
    "PaperProtectionObservation",
    "PaperState",
    "PaperStateError",
)
