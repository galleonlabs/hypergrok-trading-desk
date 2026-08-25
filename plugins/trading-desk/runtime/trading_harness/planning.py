"""Protected trade plans and conservative risk/reward tickets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
from typing import Any, Mapping

from .analysis import TechnicalBias, TechnicalSnapshot
from .assessment import AssessmentVerdict, OpportunityAssessment
from .canonical import (
    canonical_data,
    canonical_decimal,
    canonical_json,
    domain_hash,
    semantic_intent_hash,
)
from .domain import Environment, OrderType, SemanticIntent, Side
from .errors import ValidationError
from .execution_grant import SignedInfrastructureGrant, TrustedInfrastructureGrant
from .policy import BASIS_POINTS, exact_decimal
from .registered_decision import RegisteredOpportunityAssessment, RegisteredVerdict
from .strategy import CANDIDATE_V0, RegisteredStrategy


_ZERO = Decimal("0")
_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN, Emin=-192, Emax=192)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be a non-empty, trimmed string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    parsed = _text(value, field, maximum=64)
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return parsed


def _positive(value: Decimal | str | int, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed <= _ZERO:
        raise ValidationError(f"{field} must be greater than zero")
    return parsed


def _nonnegative(value: Decimal | str | int, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed < _ZERO:
        raise ValidationError(f"{field} must not be negative")
    return parsed


def _hash_payload(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    with localcontext(_CONTEXT) as context:
        units = context.divide(value, step).to_integral_value(rounding=ROUND_FLOOR)
        return context.multiply(units, step)


def _cloid(plan_seed: str, leg: str) -> str:
    digest = hashlib.sha256(f"protected-plan-v1:{plan_seed}:{leg}".encode()).hexdigest()
    return "0x" + digest[:32]


class GroupingPolicy(str, Enum):
    NORMAL_TPSL = "normalTpsl"


class RiskTicketStatus(str, Enum):
    DENIED = "denied"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass(frozen=True, slots=True)
class PlanIdentity:
    thesis_id: str
    thesis_version: str
    strategy_version: str
    venue: str
    account_id: str
    environment: Environment
    instrument: str

    def __post_init__(self) -> None:
        for field in (
            "thesis_id",
            "thesis_version",
            "strategy_version",
            "venue",
            "account_id",
            "instrument",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid plan environment") from error


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    account_id: str
    environment: Environment
    observed_at: datetime
    received_at: datetime
    equity: Decimal
    available_collateral: Decimal
    daily_loss_remaining: Decimal
    open_risk_remaining: Decimal
    max_notional: Decimal
    lot_size: Decimal
    leverage: Decimal
    artifact_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid account environment") from error
        observed = _utc(self.observed_at, "observed_at")
        received = _utc(self.received_at, "received_at")
        if received < observed:
            raise ValidationError("account receipt cannot predate observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)
        for field in (
            "equity",
            "available_collateral",
            "max_notional",
            "lot_size",
            "leverage",
        ):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        for field in ("daily_loss_remaining", "open_risk_remaining"):
            object.__setattr__(self, field, _nonnegative(getattr(self, field), field))
        object.__setattr__(self, "artifact_hash", _sha256(self.artifact_hash, "artifact_hash"))

    def is_fresh(self, at: datetime, *, maximum_age_seconds: int) -> bool:
        checked = _utc(at, "at")
        return (
            self.observed_at <= self.received_at <= checked
            and checked - self.received_at <= timedelta(seconds=maximum_age_seconds)
        )


@dataclass(frozen=True, slots=True)
class RiskSizingPolicy:
    version: str = "initial-testnet-risk-v1"
    risk_fraction: Decimal = Decimal("0.0025")
    minimum_net_reward_risk: Decimal = Decimal("2")
    entry_slippage_bps: Decimal = Decimal("20")
    exit_slippage_bps: Decimal = Decimal("50")
    stop_gap_bps: Decimal = Decimal("100")
    round_trip_fee_bps: Decimal = Decimal("10")
    max_leverage: Decimal = Decimal("2")
    account_max_age_seconds: int = 5
    ticket_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version", maximum=64))
        risk_fraction = _positive(self.risk_fraction, "risk_fraction")
        if risk_fraction > Decimal("0.0025"):
            raise ValidationError("initial risk_fraction cannot exceed 0.25%")
        object.__setattr__(self, "risk_fraction", risk_fraction)
        object.__setattr__(
            self,
            "minimum_net_reward_risk",
            _positive(self.minimum_net_reward_risk, "minimum_net_reward_risk"),
        )
        for field in (
            "entry_slippage_bps",
            "exit_slippage_bps",
            "stop_gap_bps",
            "round_trip_fee_bps",
        ):
            value = _nonnegative(getattr(self, field), field)
            if value > Decimal("2500"):
                raise ValidationError(f"{field} exceeds the compiled research bound")
            object.__setattr__(self, field, value)
        leverage = _positive(self.max_leverage, "max_leverage")
        if leverage > Decimal("2"):
            raise ValidationError("initial max_leverage cannot exceed 2")
        object.__setattr__(self, "max_leverage", leverage)
        for field in ("account_max_age_seconds", "ticket_ttl_seconds"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValidationError(f"{field} must be a positive integer")

    @property
    def policy_hash(self) -> str:
        return domain_hash("trading-harness/risk-sizing-policy/v1", self)


@dataclass(frozen=True, slots=True)
class ProtectedTradePlan:
    assessment_hash: str
    entry: SemanticIntent
    protective_stop: SemanticIntent
    take_profit: SemanticIntent
    grouping: GroupingPolicy
    plan_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessment_hash", _sha256(self.assessment_hash, "assessment_hash"))
        if not all(
            isinstance(leg, SemanticIntent)
            for leg in (self.entry, self.protective_stop, self.take_profit)
        ):
            raise TypeError("all protected plan legs must be SemanticIntent")
        if not isinstance(self.grouping, GroupingPolicy):
            try:
                object.__setattr__(self, "grouping", GroupingPolicy(self.grouping))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid grouping policy") from error
        if self.grouping is not GroupingPolicy.NORMAL_TPSL:
            raise ValidationError("initial protected plans require normalTpsl grouping")
        entry = self.entry
        stop = self.protective_stop
        target = self.take_profit
        shared = (
            "thesis_id",
            "thesis_version",
            "strategy_version",
            "code_hash",
            "venue",
            "account_id",
            "environment",
            "instrument",
        )
        for field in shared:
            if getattr(stop, field) != getattr(entry, field) or getattr(target, field) != getattr(
                entry, field
            ):
                raise ValidationError(f"protected plan legs disagree on {field}")
        if entry.reduce_only:
            raise ValidationError("risk-increasing entry cannot be reduce-only")
        if entry.order_type is not OrderType.MARKET:
            raise ValidationError("initial protected entry must be a bounded market intent")
        if stop.order_type is not OrderType.STOP or target.order_type is not OrderType.STOP:
            raise ValidationError("protected exits must be trigger intents")
        if entry.action != "place_order" or stop.action != "place_stop" or target.action != "place_take_profit":
            raise ValidationError("protected plan actions are invalid")
        if stop.side is entry.side or target.side is entry.side:
            raise ValidationError("stop and take-profit must oppose the entry side")
        if not stop.reduce_only or not target.reduce_only:
            raise ValidationError("stop and take-profit must be reduce-only")
        if stop.quantity != entry.quantity or target.quantity != entry.quantity:
            raise ValidationError("protected leg quantities must match the entry")
        if stop.stop_price is None or target.stop_price is None:
            raise ValidationError("protected plans require stop and target triggers")
        if stop.price_bound is None or stop.protection_limit_price is None:
            raise ValidationError("protective stop requires a stressed executable bound")
        if target.price_bound is None:
            raise ValidationError("take-profit requires a bounded executable price")
        entry_price = entry.price_bound
        if entry_price is None:
            raise ValidationError("entry requires a bounded worst fill price")
        if entry.side is Side.BUY:
            if not stop.stop_price < entry_price < target.stop_price:
                raise ValidationError("long stop/entry/target prices are not ordered")
            if not stop.price_bound <= stop.stop_price:
                raise ValidationError("long stop bound must not exceed its trigger")
            if not target.price_bound > entry_price:
                raise ValidationError("long target bound must remain rewarding")
        else:
            if not target.stop_price < entry_price < stop.stop_price:
                raise ValidationError("short target/entry/stop prices are not ordered")
            if not stop.price_bound >= stop.stop_price:
                raise ValidationError("short stop bound must not be below its trigger")
            if not target.price_bound < entry_price:
                raise ValidationError("short target bound must remain rewarding")
        if len({entry.client_order_id, stop.client_order_id, target.client_order_id}) != 3:
            raise ValidationError("every protected leg requires a unique client order ID")
        for client_order_id in (
            entry.client_order_id,
            stop.client_order_id,
            target.client_order_id,
        ):
            if (
                len(client_order_id) != 34
                or not client_order_id.startswith("0x")
                or any(character not in "0123456789abcdef" for character in client_order_id[2:])
            ):
                raise ValidationError("protected leg client order IDs must be 128-bit hex")
        expected = _hash_payload(
            {
                "domain": "protected-trade-plan-v1",
                "assessment_hash": self.assessment_hash,
                "grouping": self.grouping.value,
                "legs": [
                    canonical_data(entry),
                    canonical_data(stop),
                    canonical_data(target),
                ],
            }
        )
        if self.plan_hash != expected:
            raise ValidationError("plan_hash does not match the protected plan")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "protected_trade_plan.v1",
            "assessment_hash": self.assessment_hash,
            "grouping": self.grouping.value,
            "plan_hash": self.plan_hash,
            "entry_hash": semantic_intent_hash(self.entry),
            "stop_hash": semantic_intent_hash(self.protective_stop),
            "take_profit_hash": semantic_intent_hash(self.take_profit),
            "entry": canonical_data(self.entry),
            "protective_stop": canonical_data(self.protective_stop),
            "take_profit": canonical_data(self.take_profit),
            "stop_mandatory": True,
        }


@dataclass(frozen=True, slots=True)
class RiskTicket:
    ticket_id: str
    assessment_hash: str
    account_snapshot_hash: str
    policy_version: str
    policy_hash: str
    created_at: datetime
    expires_at: datetime
    status: RiskTicketStatus
    reason_codes: tuple[str, ...]
    risk_budget: Decimal
    quantity: Decimal
    expected_loss: Decimal
    stressed_loss: Decimal
    expected_reward: Decimal
    net_reward_risk: Decimal | None
    catastrophic_loss_bound: Decimal
    plan: ProtectedTradePlan | None
    ticket_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticket_id", _text(self.ticket_id, "ticket_id"))
        object.__setattr__(self, "assessment_hash", _sha256(self.assessment_hash, "assessment_hash"))
        object.__setattr__(
            self,
            "account_snapshot_hash",
            _sha256(self.account_snapshot_hash, "account_snapshot_hash"),
        )
        object.__setattr__(self, "policy_version", _text(self.policy_version, "policy_version"))
        object.__setattr__(self, "policy_hash", _sha256(self.policy_hash, "policy_hash"))
        created = _utc(self.created_at, "created_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= created:
            raise ValidationError("risk ticket must expire after creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        if not isinstance(self.status, RiskTicketStatus):
            try:
                object.__setattr__(self, "status", RiskTicketStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid risk ticket status") from error
        if not self.reason_codes or any(
            not isinstance(reason, str) or not reason or reason != reason.strip()
            for reason in self.reason_codes
        ):
            raise ValidationError("risk ticket requires stable reason codes")
        for field in (
            "risk_budget",
            "quantity",
            "expected_loss",
            "stressed_loss",
            "expected_reward",
            "catastrophic_loss_bound",
        ):
            object.__setattr__(self, field, _nonnegative(getattr(self, field), field))
        if self.net_reward_risk is not None:
            object.__setattr__(
                self,
                "net_reward_risk",
                _nonnegative(self.net_reward_risk, "net_reward_risk"),
            )
        if self.status is RiskTicketStatus.DENIED:
            if self.plan is not None or self.quantity != _ZERO:
                raise ValidationError("denied risk tickets cannot carry an executable plan")
        else:
            if not isinstance(self.plan, ProtectedTradePlan):
                raise ValidationError("passing risk tickets require a protected plan")
            if self.quantity <= _ZERO or self.stressed_loss <= _ZERO:
                raise ValidationError("passing risk tickets require positive bounded risk")
            if self.net_reward_risk is None:
                raise ValidationError("passing risk tickets require net reward/risk")
        payload = {
            "domain": "risk-ticket-v1",
            "ticket_id": self.ticket_id,
            "assessment_hash": self.assessment_hash,
            "account_snapshot_hash": self.account_snapshot_hash,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "created_at": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "risk_budget": canonical_decimal(self.risk_budget),
            "quantity": canonical_decimal(self.quantity),
            "expected_loss": canonical_decimal(self.expected_loss),
            "stressed_loss": canonical_decimal(self.stressed_loss),
            "expected_reward": canonical_decimal(self.expected_reward),
            "net_reward_risk": (
                None if self.net_reward_risk is None else canonical_decimal(self.net_reward_risk)
            ),
            "catastrophic_loss_bound": canonical_decimal(self.catastrophic_loss_bound),
            "plan_hash": None if self.plan is None else self.plan.plan_hash,
        }
        expected_hash = _hash_payload(payload)
        if self.ticket_hash:
            supplied_hash = _sha256(self.ticket_hash, "ticket_hash")
            if supplied_hash != expected_hash:
                raise ValidationError("ticket_hash does not match the risk ticket")
        object.__setattr__(self, "ticket_hash", expected_hash)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "risk_ticket.v1",
            "ticket_id": self.ticket_id,
            "assessment_hash": self.assessment_hash,
            "account_snapshot_hash": self.account_snapshot_hash,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "created_at": self.created_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "expires_at": self.expires_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "risk_budget": canonical_decimal(self.risk_budget),
            "quantity": canonical_decimal(self.quantity),
            "expected_loss": canonical_decimal(self.expected_loss),
            "stressed_loss": canonical_decimal(self.stressed_loss),
            "expected_reward": canonical_decimal(self.expected_reward),
            "net_reward_risk": (
                None
                if self.net_reward_risk is None
                else canonical_decimal(self.net_reward_risk)
            ),
            "catastrophic_loss_bound": canonical_decimal(self.catastrophic_loss_bound),
            "plan": None if self.plan is None else self.plan.as_dict(),
            "ticket_hash": self.ticket_hash,
            "approval_created": False,
            "eligible_to_trade": False,
            "order_submitted": False,
        }


def _denied_ticket(
    *,
    ticket_id: str,
    assessment: OpportunityAssessment | RegisteredOpportunityAssessment,
    account: AccountRiskSnapshot,
    policy: RiskSizingPolicy,
    at: datetime,
    reasons: list[str],
) -> RiskTicket:
    return RiskTicket(
        ticket_id=ticket_id,
        assessment_hash=assessment.artifact_hash,
        account_snapshot_hash=account.artifact_hash,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        created_at=at,
        expires_at=at + timedelta(seconds=policy.ticket_ttl_seconds),
        status=RiskTicketStatus.DENIED,
        reason_codes=tuple(reasons),
        risk_budget=_ZERO,
        quantity=_ZERO,
        expected_loss=_ZERO,
        stressed_loss=_ZERO,
        expected_reward=_ZERO,
        net_reward_risk=None,
        catastrophic_loss_bound=account.equity,
        plan=None,
        ticket_hash="",
    )


def quote_risk_ticket(
    *,
    ticket_id: str,
    assessment: OpportunityAssessment | RegisteredOpportunityAssessment,
    technical: TechnicalSnapshot | None,
    identity: PlanIdentity,
    account: AccountRiskSnapshot,
    at: datetime,
    policy: RiskSizingPolicy = RiskSizingPolicy(),
    strategy: RegisteredStrategy | None = None,
    _infrastructure_learning: bool = False,
) -> RiskTicket:
    if not isinstance(
        assessment,
        (OpportunityAssessment, RegisteredOpportunityAssessment),
    ):
        raise TypeError("assessment must be a supported opportunity assessment")
    if not isinstance(identity, PlanIdentity):
        raise TypeError("identity must be PlanIdentity")
    if not isinstance(account, AccountRiskSnapshot):
        raise TypeError("account must be AccountRiskSnapshot")
    if not isinstance(policy, RiskSizingPolicy):
        raise TypeError("policy must be RiskSizingPolicy")
    if type(_infrastructure_learning) is not bool:
        raise TypeError("_infrastructure_learning must be bool")
    checked_ticket = _text(ticket_id, "ticket_id")
    checked_at = _utc(at, "at")
    reasons: list[str] = []
    signal_hash: str | None = None
    if isinstance(assessment, OpportunityAssessment):
        if not isinstance(technical, TechnicalSnapshot):
            raise TypeError("legacy assessment requires TechnicalSnapshot")
        if strategy is not None:
            raise TypeError("legacy assessment cannot carry RegisteredStrategy")
        directional = assessment.verdict in {
            AssessmentVerdict.BUY,
            AssessmentVerdict.SELL,
        }
        side = Side.BUY if assessment.verdict is AssessmentVerdict.BUY else Side.SELL
        entry = technical.close
        stop = technical.stop_price
        target = technical.target_price
        strategy_version = technical.config_version
        code_hash = technical.config_hash
        technical_hash = _hash_payload(technical.as_dict())
        if assessment.technical_hash != technical_hash:
            reasons.append("technical_artifact_mismatch")
    else:
        if technical is not None:
            raise TypeError("registered assessment does not accept descriptive technical input")
        if not isinstance(strategy, RegisteredStrategy):
            raise TypeError("registered assessment requires RegisteredStrategy")
        directional = assessment.verdict in {
            RegisteredVerdict.BUY,
            RegisteredVerdict.SELL,
        }
        side = Side.BUY if assessment.verdict is RegisteredVerdict.BUY else Side.SELL
        entry = assessment.reference_price
        stop = assessment.stop_price
        target = assessment.target_price
        strategy_version = strategy.strategy_version
        code_hash = strategy.registration_hash
        signal_hash = assessment.signal_hash
        if assessment.strategy_hash != strategy.registration_hash:
            reasons.append("registered_strategy_hash_mismatch")
        if identity.instrument not in {
            assessment.instrument,
            f"{assessment.instrument}-PERP",
        }:
            reasons.append("registered_asset_scope_mismatch")
    if not directional:
        reasons.append("assessment_not_directional")
    learning_eligible = (
        _infrastructure_learning
        and isinstance(assessment, RegisteredOpportunityAssessment)
        and directional
        and identity.environment is Environment.TESTNET
    )
    if not assessment.eligible_for_risk_quote and not learning_eligible:
        reasons.append("assessment_not_risk_eligible")
    if not assessment.is_fresh(checked_at):
        reasons.append("assessment_stale")
    if identity.account_id != account.account_id or identity.environment is not account.environment:
        reasons.append("account_scope_mismatch")
    if identity.strategy_version != strategy_version:
        reasons.append("strategy_version_mismatch")
    if not account.is_fresh(checked_at, maximum_age_seconds=policy.account_max_age_seconds):
        reasons.append("account_snapshot_stale")
    if account.leverage > policy.max_leverage:
        reasons.append("leverage_limit")
    if entry is None or stop is None or target is None:
        reasons.append("mandatory_stop_or_target_missing")
    if (
        identity.environment is Environment.MAINNET
        and policy.risk_fraction > Decimal("0.001")
    ):
        reasons.append("mainnet_canary_risk_fraction_exceeded")
    if reasons:
        return _denied_ticket(
            ticket_id=checked_ticket,
            assessment=assessment,
            account=account,
            policy=policy,
            at=checked_at,
            reasons=reasons,
        )

    assert entry is not None and stop is not None and target is not None
    with localcontext(_CONTEXT) as context:
        entry_slippage = context.divide(policy.entry_slippage_bps, BASIS_POINTS)
        exit_stress = context.divide(
            context.add(policy.exit_slippage_bps, policy.stop_gap_bps),
            BASIS_POINTS,
        )
        target_slippage = context.divide(policy.exit_slippage_bps, BASIS_POINTS)
        fees = context.divide(policy.round_trip_fee_bps, BASIS_POINTS)
        if side is Side.BUY:
            worst_entry = context.multiply(entry, context.add(Decimal(1), entry_slippage))
            stressed_stop = context.multiply(stop, context.subtract(Decimal(1), exit_stress))
            worst_target = context.multiply(target, context.subtract(Decimal(1), target_slippage))
        else:
            worst_entry = context.multiply(entry, context.subtract(Decimal(1), entry_slippage))
            stressed_stop = context.multiply(stop, context.add(Decimal(1), exit_stress))
            worst_target = context.multiply(target, context.add(Decimal(1), target_slippage))
        fee_per_unit = context.multiply(entry, fees)
        loss_per_unit = context.add(abs(context.subtract(worst_entry, stressed_stop)), fee_per_unit)
        reward_per_unit = context.subtract(
            abs(context.subtract(worst_target, worst_entry)),
            fee_per_unit,
        )
        risk_budget = min(
            context.multiply(account.equity, policy.risk_fraction),
            account.daily_loss_remaining,
            account.open_risk_remaining,
        )
        if loss_per_unit <= _ZERO or reward_per_unit <= _ZERO or risk_budget <= _ZERO:
            reasons.append("non_positive_trade_economics")
            raw_quantity = _ZERO
        else:
            raw_quantity = context.divide(risk_budget, loss_per_unit)
        notional_cap = min(
            account.max_notional,
            context.multiply(account.available_collateral, account.leverage),
        )
        if worst_entry <= _ZERO:
            reasons.append("invalid_entry_bound")
            notional_quantity = _ZERO
        else:
            notional_quantity = context.divide(notional_cap, worst_entry)
        quantity = _floor_to_step(min(raw_quantity, notional_quantity), account.lot_size)
        if quantity <= _ZERO:
            reasons.append("quantity_below_lot")
        stressed_loss = context.multiply(quantity, loss_per_unit)
        expected_loss = context.multiply(quantity, abs(context.subtract(entry, stop)))
        expected_reward = context.multiply(quantity, reward_per_unit)
        net_rr = (
            None
            if stressed_loss <= _ZERO
            else context.divide(expected_reward, stressed_loss)
        )
        if net_rr is None or net_rr < policy.minimum_net_reward_risk:
            reasons.append("net_reward_risk_below_minimum")
        if stressed_loss > risk_budget:
            reasons.append("risk_budget_exceeded")

    if reasons:
        return _denied_ticket(
            ticket_id=checked_ticket,
            assessment=assessment,
            account=account,
            policy=policy,
            at=checked_at,
            reasons=sorted(set(reasons)),
        )

    expires = min(
        assessment.expires_at,
        checked_at + timedelta(seconds=policy.ticket_ttl_seconds),
    )
    plan_seed = f"{assessment.artifact_hash}:{account.artifact_hash}:{checked_ticket}"
    entry_id = _cloid(plan_seed, "entry")
    stop_id = _cloid(plan_seed, "stop")
    target_id = _cloid(plan_seed, "target")
    common = {
        "thesis_id": identity.thesis_id,
        "thesis_version": identity.thesis_version,
        "strategy_version": identity.strategy_version,
        "code_hash": code_hash,
        "venue": identity.venue,
        "account_id": identity.account_id,
        "environment": identity.environment,
        "instrument": identity.instrument,
        "quantity": quantity,
        "expires_at": expires,
        "leverage": account.leverage,
        "fee_bps": policy.round_trip_fee_bps,
    }
    if signal_hash is not None:
        common["signal_instance_hash"] = signal_hash
    entry_intent = SemanticIntent(
        intent_id=f"{checked_ticket}-entry",
        action="place_order",
        side=side,
        order_type=OrderType.MARKET,
        client_order_id=entry_id,
        price_bound=worst_entry,
        stop_price=stop,
        protection_limit_price=stressed_stop,
        max_slippage_bps=policy.entry_slippage_bps,
        time_in_force="Ioc",
        **common,
    )
    exit_side = Side.SELL if side is Side.BUY else Side.BUY
    stop_intent = SemanticIntent(
        intent_id=f"{checked_ticket}-stop",
        action="place_stop",
        side=exit_side,
        order_type=OrderType.STOP,
        client_order_id=stop_id,
        price_bound=stressed_stop,
        stop_price=stop,
        protection_limit_price=stressed_stop,
        reduce_only=True,
        max_slippage_bps=policy.exit_slippage_bps + policy.stop_gap_bps,
        **common,
    )
    take_profit_intent = SemanticIntent(
        intent_id=f"{checked_ticket}-take-profit",
        action="place_take_profit",
        side=exit_side,
        order_type=OrderType.STOP,
        client_order_id=target_id,
        price_bound=worst_target,
        stop_price=target,
        reduce_only=True,
        max_slippage_bps=policy.exit_slippage_bps,
        **common,
    )
    plan_payload = {
        "domain": "protected-trade-plan-v1",
        "assessment_hash": assessment.artifact_hash,
        "grouping": GroupingPolicy.NORMAL_TPSL.value,
        "legs": [
            canonical_data(entry_intent),
            canonical_data(stop_intent),
            canonical_data(take_profit_intent),
        ],
    }
    plan = ProtectedTradePlan(
        assessment_hash=assessment.artifact_hash,
        entry=entry_intent,
        protective_stop=stop_intent,
        take_profit=take_profit_intent,
        grouping=GroupingPolicy.NORMAL_TPSL,
        plan_hash=_hash_payload(plan_payload),
    )
    return RiskTicket(
        ticket_id=checked_ticket,
        assessment_hash=assessment.artifact_hash,
        account_snapshot_hash=account.artifact_hash,
        policy_version=policy.version,
        policy_hash=policy.policy_hash,
        created_at=checked_at,
        expires_at=expires,
        status=RiskTicketStatus.AWAITING_APPROVAL,
        reason_codes=("risk_checks_passed", "trusted_approval_required"),
        risk_budget=risk_budget,
        quantity=quantity,
        expected_loss=expected_loss,
        stressed_loss=stressed_loss,
        expected_reward=expected_reward,
        net_reward_risk=net_rr,
        catastrophic_loss_bound=account.equity,
        plan=plan,
        ticket_hash="",
    )


def quote_infrastructure_learning_ticket(
    *,
    ticket_id: str,
    assessment: RegisteredOpportunityAssessment,
    identity: PlanIdentity,
    account: AccountRiskSnapshot,
    grant: SignedInfrastructureGrant | TrustedInfrastructureGrant,
    at: datetime,
    policy: RiskSizingPolicy = RiskSizingPolicy(),
    strategy: RegisteredStrategy = CANDIDATE_V0,
) -> RiskTicket:
    """Quote a bounded TESTNET learning ticket without a profitability claim."""

    if not isinstance(assessment, RegisteredOpportunityAssessment):
        raise TypeError("learning quote requires RegisteredOpportunityAssessment")
    if not isinstance(
        grant, (SignedInfrastructureGrant, TrustedInfrastructureGrant)
    ):
        raise TypeError("grant must be a signed or trusted infrastructure scope")
    checked_at = _utc(at, "at")
    if (
        identity.environment is not Environment.TESTNET
        or account.environment is not Environment.TESTNET
        or grant.environment is not Environment.TESTNET
        or grant.account_id != identity.account_id
        or grant.account_id != account.account_id
        or identity.instrument not in grant.allowed_instruments
        or identity.instrument not in {
            assessment.instrument,
            f"{assessment.instrument}-PERP",
        }
        or policy.policy_hash != grant.risk_policy_hash
        or not grant.is_active(checked_at)
    ):
        raise ValidationError("learning quote differs from infrastructure grant scope")
    ticket = quote_risk_ticket(
        ticket_id=ticket_id,
        assessment=assessment,
        technical=None,
        identity=identity,
        account=account,
        at=checked_at,
        policy=policy,
        strategy=strategy,
        _infrastructure_learning=True,
    )
    if ticket.status is RiskTicketStatus.AWAITING_APPROVAL:
        assert ticket.plan is not None
        entry = ticket.plan.entry
        assert entry.price_bound is not None
        notional = entry.quantity * entry.price_bound
        if (
            ticket.stressed_loss > grant.max_loss
            or notional > grant.max_notional
            or entry.leverage is None
            or entry.leverage > grant.max_leverage
        ):
            raise ValidationError("learning ticket exceeds infrastructure grant caps")
    return ticket


def _parse_instant(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 string") from error
    return _utc(parsed, field)


def protected_trade_plan_from_dict(value: Mapping[str, Any]) -> ProtectedTradePlan:
    """Reconstruct and verify one persisted public protected-plan document."""

    if not isinstance(value, Mapping):
        raise TypeError("protected plan document must be a mapping")
    document = dict(value)
    expected = {
        "schema_version",
        "assessment_hash",
        "grouping",
        "plan_hash",
        "entry_hash",
        "stop_hash",
        "take_profit_hash",
        "entry",
        "protective_stop",
        "take_profit",
        "stop_mandatory",
    }
    if set(document) != expected:
        raise ValidationError("protected plan document fields are unsupported")
    if document["schema_version"] != "protected_trade_plan.v1":
        raise ValidationError("protected plan schema version is unsupported")
    if document["stop_mandatory"] is not True:
        raise ValidationError("persisted protected plan must require its stop")
    legs: list[SemanticIntent] = []
    for field in ("entry", "protective_stop", "take_profit"):
        raw = document[field]
        if not isinstance(raw, Mapping):
            raise ValidationError(f"{field} must be an intent object")
        try:
            legs.append(SemanticIntent.from_mapping(raw))
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError(f"{field} is not a valid semantic intent") from error
    entry, stop, target = legs
    supplied_hashes = (
        _sha256(document["entry_hash"], "entry_hash"),
        _sha256(document["stop_hash"], "stop_hash"),
        _sha256(document["take_profit_hash"], "take_profit_hash"),
    )
    actual_hashes = tuple(semantic_intent_hash(leg) for leg in legs)
    if supplied_hashes != actual_hashes:
        raise ValidationError("protected plan leg hash does not match its intent")
    return ProtectedTradePlan(
        assessment_hash=_sha256(document["assessment_hash"], "assessment_hash"),
        entry=entry,
        protective_stop=stop,
        take_profit=target,
        grouping=GroupingPolicy(document["grouping"]),
        plan_hash=_sha256(document["plan_hash"], "plan_hash"),
    )


def risk_ticket_from_dict(value: Mapping[str, Any]) -> RiskTicket:
    """Reconstruct and integrity-check one persisted public risk ticket."""

    if not isinstance(value, Mapping):
        raise TypeError("risk ticket document must be a mapping")
    document = dict(value)
    expected = {
        "schema_version",
        "ticket_id",
        "assessment_hash",
        "account_snapshot_hash",
        "policy_version",
        "policy_hash",
        "created_at",
        "expires_at",
        "status",
        "reason_codes",
        "risk_budget",
        "quantity",
        "expected_loss",
        "stressed_loss",
        "expected_reward",
        "net_reward_risk",
        "catastrophic_loss_bound",
        "plan",
        "ticket_hash",
        "approval_created",
        "eligible_to_trade",
        "order_submitted",
    }
    if set(document) != expected:
        raise ValidationError("risk ticket document fields are unsupported")
    if document["schema_version"] != "risk_ticket.v1":
        raise ValidationError("risk ticket schema version is unsupported")
    if any(
        document[field] is not False
        for field in ("approval_created", "eligible_to_trade", "order_submitted")
    ):
        raise ValidationError("persisted risk ticket contains unsupported authority")
    reasons = document["reason_codes"]
    if not isinstance(reasons, list):
        raise ValidationError("risk ticket reason_codes must be an array")
    plan_document = document["plan"]
    if plan_document is not None and not isinstance(plan_document, Mapping):
        raise ValidationError("risk ticket plan must be an object or null")
    net_rr = document["net_reward_risk"]
    if net_rr is not None and not isinstance(net_rr, (str, int, Decimal)):
        raise ValidationError("net_reward_risk must be an exact decimal or null")
    try:
        status = RiskTicketStatus(document["status"])
    except (TypeError, ValueError) as error:
        raise ValidationError("risk ticket status is unsupported") from error
    return RiskTicket(
        ticket_id=document["ticket_id"],
        assessment_hash=document["assessment_hash"],
        account_snapshot_hash=document["account_snapshot_hash"],
        policy_version=document["policy_version"],
        policy_hash=document["policy_hash"],
        created_at=_parse_instant(document["created_at"], "created_at"),
        expires_at=_parse_instant(document["expires_at"], "expires_at"),
        status=status,
        reason_codes=tuple(reasons),
        risk_budget=document["risk_budget"],
        quantity=document["quantity"],
        expected_loss=document["expected_loss"],
        stressed_loss=document["stressed_loss"],
        expected_reward=document["expected_reward"],
        net_reward_risk=net_rr,
        catastrophic_loss_bound=document["catastrophic_loss_bound"],
        plan=(
            None
            if plan_document is None
            else protected_trade_plan_from_dict(plan_document)
        ),
        ticket_hash=document["ticket_hash"],
    )


__all__ = (
    "AccountRiskSnapshot",
    "GroupingPolicy",
    "PlanIdentity",
    "ProtectedTradePlan",
    "RiskSizingPolicy",
    "RiskTicket",
    "RiskTicketStatus",
    "quote_risk_ticket",
    "quote_infrastructure_learning_ticket",
    "protected_trade_plan_from_dict",
    "risk_ticket_from_dict",
)
