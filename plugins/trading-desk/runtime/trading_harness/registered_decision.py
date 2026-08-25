"""Buy/sell/nothing decisions for the frozen registered strategy.

The descriptive EMA/RSI analysis in :mod:`analysis` is intentionally not used
here.  This module accepts only a hash-bound ``candidate-v0`` signal and a
profitability attestation for that exact strategy.  A directional assessment
can be shown as research before promotion, but it cannot reach risk sizing
until historical, prospective-shadow, sentiment-increment, drift, and
freshness gates all pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
import re
from typing import Any

from .canonical import canonical_decimal, domain_hash, validate_decimal_bounds
from .errors import ValidationError
from .sentiment import CollectionMethod, SentimentLabel, SentimentSnapshot
from .strategy import CANDIDATE_V0, RegisteredStrategy, SignalDirection, StrategySignal


_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN, Emin=-192, Emax=192)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")


class RegisteredVerdict(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NOTHING = "nothing"
    UNAVAILABLE = "unavailable"


class ProfitabilityAttestationStatus(str, Enum):
    QUALIFIED = "qualified"
    INCONCLUSIVE = "inconclusive"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    RETIRED = "retired"


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


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal, int, or exact string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        validate_decimal_bounds(parsed, field=field)
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ValidationError(f"{field} must be a bounded finite decimal") from error
    return parsed


@dataclass(frozen=True, slots=True)
class ProfitabilityAttestation:
    attestation_id: str
    asset_id: str
    strategy_hash: str
    validation_artifact_hash: str
    shadow_artifact_hash: str
    status: ProfitabilityAttestationStatus
    issued_at: datetime
    expires_at: datetime
    oos_trades: int
    oos_expectancy_r: Decimal
    oos_lower_bound_r: Decimal
    shadow_closed_signals: int
    shadow_elapsed_days: int
    shadow_expectancy_r: Decimal
    shadow_lower_bound_r: Decimal
    cost_stress_positive: bool
    sentiment_incremental_passed: bool
    drift_passed: bool
    independent_reviewed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "attestation_id", _text(self.attestation_id, "attestation_id"))
        object.__setattr__(self, "asset_id", _text(self.asset_id, "asset_id"))
        for field in (
            "strategy_hash",
            "validation_artifact_hash",
            "shadow_artifact_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        if not isinstance(self.status, ProfitabilityAttestationStatus):
            try:
                object.__setattr__(
                    self,
                    "status",
                    ProfitabilityAttestationStatus(self.status),
                )
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid profitability attestation status") from error
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValidationError("profitability attestation must expire after issuance")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        for field in ("oos_trades", "shadow_closed_signals", "shadow_elapsed_days"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValidationError(f"{field} must be a non-negative integer")
        for field in (
            "oos_expectancy_r",
            "oos_lower_bound_r",
            "shadow_expectancy_r",
            "shadow_lower_bound_r",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), field))
        for field in (
            "cost_stress_positive",
            "sentiment_incremental_passed",
            "drift_passed",
            "independent_reviewed",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")

    @property
    def artifact_hash(self) -> str:
        return domain_hash("trading-harness/profitability-attestation/v1", self)

    def is_active(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return (
            self.status is ProfitabilityAttestationStatus.QUALIFIED
            and self.issued_at <= checked < self.expires_at
            and self.oos_trades >= 100
            and self.oos_expectancy_r > _ZERO
            and self.oos_lower_bound_r > _ZERO
            and self.shadow_closed_signals >= 50
            and self.shadow_elapsed_days >= 90
            and self.shadow_expectancy_r > _ZERO
            and self.shadow_lower_bound_r > _ZERO
            and self.cost_stress_positive
            and self.sentiment_incremental_passed
            and self.drift_passed
            and self.independent_reviewed
        )


@dataclass(frozen=True, slots=True)
class RegisteredOpportunityAssessment:
    assessment_id: str
    asset_id: str
    instrument: str
    created_at: datetime
    expires_at: datetime
    strategy_hash: str
    signal_hash: str
    sentiment_hash: str
    profitability_attestation_hash: str | None
    verdict: RegisteredVerdict
    reason_codes: tuple[str, ...]
    reference_price: Decimal | None
    stop_price: Decimal | None
    target_price: Decimal | None
    eligible_for_risk_quote: bool
    eligible_to_trade: bool
    artifact_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessment_id", _text(self.assessment_id, "assessment_id"))
        object.__setattr__(self, "asset_id", _text(self.asset_id, "asset_id"))
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument", maximum=64))
        created = _utc(self.created_at, "created_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires < created:
            raise ValidationError("assessment expiry cannot predate creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        for field in ("strategy_hash", "signal_hash", "sentiment_hash"):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        if self.profitability_attestation_hash is not None:
            object.__setattr__(
                self,
                "profitability_attestation_hash",
                _hash(
                    self.profitability_attestation_hash,
                    "profitability_attestation_hash",
                ),
            )
        if not isinstance(self.verdict, RegisteredVerdict):
            try:
                object.__setattr__(self, "verdict", RegisteredVerdict(self.verdict))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid registered verdict") from error
        if not self.reason_codes or any(
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 128
            for reason in self.reason_codes
        ):
            raise ValidationError("registered assessment requires stable reason codes")
        for field in ("reference_price", "stop_price", "target_price"):
            value = getattr(self, field)
            if value is not None:
                parsed = _decimal(value, field)
                if parsed <= _ZERO:
                    raise ValidationError(f"{field} must be positive")
                object.__setattr__(self, field, parsed)
        if type(self.eligible_for_risk_quote) is not bool or type(self.eligible_to_trade) is not bool:
            raise TypeError("assessment eligibility flags must be bool")
        if self.eligible_to_trade:
            raise ValidationError("registered assessments cannot confer trade authority")
        directional = self.verdict in {RegisteredVerdict.BUY, RegisteredVerdict.SELL}
        has_bracket = all(
            value is not None
            for value in (self.reference_price, self.stop_price, self.target_price)
        )
        if directional != has_bracket:
            raise ValidationError("only directional assessments may carry bracket prices")
        if self.eligible_for_risk_quote and not directional:
            raise ValidationError("risk-quote eligibility requires a directional bracket")
        if directional and self.verdict is RegisteredVerdict.BUY:
            if not self.stop_price < self.reference_price < self.target_price:  # type: ignore[operator]
                raise ValidationError("buy assessment bracket is not ordered")
        if directional and self.verdict is RegisteredVerdict.SELL:
            if not self.target_price < self.reference_price < self.stop_price:  # type: ignore[operator]
                raise ValidationError("sell assessment bracket is not ordered")
        expected_hash = domain_hash(
            "trading-harness/registered-opportunity-assessment/v1",
            _registered_payload(self),
        )
        if self.artifact_hash != expected_hash:
            raise ValidationError("artifact_hash does not match registered assessment")

    def is_fresh(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return self.created_at <= checked < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "registered_opportunity_assessment.v1",
            "assessment_id": self.assessment_id,
            "asset_id": self.asset_id,
            "instrument": self.instrument,
            "created_at": self.created_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "expires_at": self.expires_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "strategy_hash": self.strategy_hash,
            "signal_hash": self.signal_hash,
            "sentiment_hash": self.sentiment_hash,
            "profitability_attestation_hash": self.profitability_attestation_hash,
            "verdict": self.verdict.value,
            "reason_codes": list(self.reason_codes),
            "reference_price": (
                None if self.reference_price is None else canonical_decimal(self.reference_price)
            ),
            "stop_price": None if self.stop_price is None else canonical_decimal(self.stop_price),
            "target_price": (
                None if self.target_price is None else canonical_decimal(self.target_price)
            ),
            "eligible_for_risk_quote": self.eligible_for_risk_quote,
            "eligible_to_trade": self.eligible_to_trade,
            "artifact_hash": self.artifact_hash,
            "approval_created": False,
            "order_submitted": False,
        }


def _registered_payload(value: RegisteredOpportunityAssessment) -> dict[str, Any]:
    return {
        "assessment_id": value.assessment_id,
        "asset_id": value.asset_id,
        "instrument": value.instrument,
        "created_at": value.created_at,
        "expires_at": value.expires_at,
        "strategy_hash": value.strategy_hash,
        "signal_hash": value.signal_hash,
        "sentiment_hash": value.sentiment_hash,
        "profitability_attestation_hash": value.profitability_attestation_hash,
        "verdict": value.verdict.value,
        "reason_codes": list(value.reason_codes),
        "reference_price": value.reference_price,
        "stop_price": value.stop_price,
        "target_price": value.target_price,
        "eligible_for_risk_quote": value.eligible_for_risk_quote,
        "eligible_to_trade": False,
    }


def build_registered_assessment(
    *,
    assessment_id: str,
    asset_id: str,
    signal: StrategySignal,
    sentiment: SentimentSnapshot,
    profitability: ProfitabilityAttestation | None,
    at: datetime,
    strategy: RegisteredStrategy = CANDIDATE_V0,
    attended: bool = False,
) -> RegisteredOpportunityAssessment:
    """Combine exact registered evidence while preserving abstention."""

    if not isinstance(signal, StrategySignal):
        raise TypeError("signal must be StrategySignal")
    if not isinstance(sentiment, SentimentSnapshot):
        raise TypeError("sentiment must be SentimentSnapshot")
    if profitability is not None and not isinstance(profitability, ProfitabilityAttestation):
        raise TypeError("profitability must be ProfitabilityAttestation or None")
    if not isinstance(strategy, RegisteredStrategy):
        raise TypeError("strategy must be RegisteredStrategy")
    if type(attended) is not bool:
        raise TypeError("attended must be bool")
    checked_id = _text(assessment_id, "assessment_id")
    checked_asset = _text(asset_id, "asset_id")
    checked_at = _utc(at, "at")
    if signal.strategy_hash != strategy.registration_hash:
        raise ValidationError("signal does not match the frozen registered strategy")
    if sentiment.asset_id != checked_asset:
        raise ValidationError("sentiment snapshot targets a different asset")

    expires = max(checked_at, min(signal.expires_at, sentiment.expires_at))
    reasons: list[str] = []
    verdict = RegisteredVerdict.NOTHING
    if signal.observed_at > checked_at:
        verdict = RegisteredVerdict.UNAVAILABLE
        reasons.append("signal_not_yet_observed")
    elif not signal.observed_at <= checked_at < signal.expires_at:
        verdict = RegisteredVerdict.UNAVAILABLE
        reasons.append("registered_signal_stale")
    elif signal.direction is SignalDirection.NOTHING:
        verdict = RegisteredVerdict.NOTHING
        reasons.append(signal.reason)
    elif not sentiment.is_fresh(checked_at):
        verdict = RegisteredVerdict.UNAVAILABLE
        reasons.append("sentiment_stale")
    elif not sentiment.available:
        verdict = RegisteredVerdict.UNAVAILABLE
        reasons.extend(sentiment.quality_reasons or ("sentiment_unavailable",))
    elif signal.direction is SignalDirection.BUY and sentiment.label is SentimentLabel.BEARISH:
        verdict = RegisteredVerdict.NOTHING
        reasons.append("bearish_sentiment_veto")
    elif signal.direction is SignalDirection.SELL and sentiment.label is SentimentLabel.BULLISH:
        verdict = RegisteredVerdict.NOTHING
        reasons.append("bullish_sentiment_veto")
    elif signal.direction is SignalDirection.BUY:
        verdict = RegisteredVerdict.BUY
        reasons.append("registered_buy_sentiment_not_bearish")
    else:
        verdict = RegisteredVerdict.SELL
        reasons.append("registered_sell_sentiment_not_bullish")

    qualified = (
        profitability is not None
        and profitability.asset_id == checked_asset
        and profitability.strategy_hash == signal.strategy_hash
        and profitability.is_active(checked_at)
    )
    if profitability is None:
        reasons.append("profitability_attestation_missing")
    elif profitability.asset_id != checked_asset:
        reasons.append("profitability_asset_mismatch")
    elif profitability.strategy_hash != signal.strategy_hash:
        reasons.append("profitability_strategy_mismatch")
    elif not profitability.is_active(checked_at):
        reasons.append("profitability_not_qualified")
    if sentiment.method is CollectionMethod.MANUAL_BROWSER:
        reasons.append(
            "manual_sentiment_requires_attended_approval"
            if attended
            else "manual_sentiment_not_unattended"
        )

    directional = verdict in {RegisteredVerdict.BUY, RegisteredVerdict.SELL}
    reference: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    if directional:
        with localcontext(_CONTEXT) as context:
            stop_distance = context.multiply(
                signal.features.atr,
                strategy.stop_atr_multiple,
            )
            target_distance = context.multiply(
                signal.features.atr,
                strategy.target_atr_multiple,
            )
            reference = signal.close
            if verdict is RegisteredVerdict.BUY:
                stop = context.subtract(reference, stop_distance)
                target = context.add(reference, target_distance)
            else:
                stop = context.add(reference, stop_distance)
                target = context.subtract(reference, target_distance)
        if stop <= _ZERO or target <= _ZERO:
            verdict = RegisteredVerdict.UNAVAILABLE
            directional = False
            reference = stop = target = None
            reasons.append("invalid_bracket_geometry")

    eligible = (
        directional
        and qualified
        and (
            sentiment.eligible_for_unattended_use
            or (
                attended
                and sentiment.method is CollectionMethod.MANUAL_BROWSER
                and sentiment.available
                and sentiment.is_fresh(checked_at)
            )
        )
        and checked_at < expires
    )
    if directional and not eligible:
        reasons.append("risk_quote_ineligible")
    # Calculate from the exact constructor values, then let __post_init__
    # independently recalculate and verify it.
    payload = {
        "assessment_id": checked_id,
        "asset_id": checked_asset,
        "instrument": signal.instrument,
        "created_at": checked_at,
        "expires_at": expires,
        "strategy_hash": signal.strategy_hash,
        "signal_hash": signal.signal_hash,
        "sentiment_hash": sentiment.artifact_hash,
        "profitability_attestation_hash": (
            None if profitability is None else profitability.artifact_hash
        ),
        "verdict": verdict.value,
        "reason_codes": reasons,
        "reference_price": reference,
        "stop_price": stop,
        "target_price": target,
        "eligible_for_risk_quote": eligible,
        "eligible_to_trade": False,
    }
    return RegisteredOpportunityAssessment(
        assessment_id=checked_id,
        asset_id=checked_asset,
        instrument=signal.instrument,
        created_at=checked_at,
        expires_at=expires,
        strategy_hash=signal.strategy_hash,
        signal_hash=signal.signal_hash,
        sentiment_hash=sentiment.artifact_hash,
        profitability_attestation_hash=(
            None if profitability is None else profitability.artifact_hash
        ),
        verdict=verdict,
        reason_codes=tuple(reasons),
        reference_price=reference,
        stop_price=stop,
        target_price=target,
        eligible_for_risk_quote=eligible,
        eligible_to_trade=False,
        artifact_hash=domain_hash(
            "trading-harness/registered-opportunity-assessment/v1",
            payload,
        ),
    )


def _parse_instant(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from error
    return _utc(parsed, field)


def registered_assessment_from_dict(
    value: dict[str, object],
) -> RegisteredOpportunityAssessment:
    """Reconstruct and integrity-check one saved registered assessment."""

    if not isinstance(value, dict):
        raise TypeError("registered assessment document must be an object")
    expected = {
        "schema_version",
        "assessment_id",
        "asset_id",
        "instrument",
        "created_at",
        "expires_at",
        "strategy_hash",
        "signal_hash",
        "sentiment_hash",
        "profitability_attestation_hash",
        "verdict",
        "reason_codes",
        "reference_price",
        "stop_price",
        "target_price",
        "eligible_for_risk_quote",
        "eligible_to_trade",
        "artifact_hash",
        "approval_created",
        "order_submitted",
    }
    if set(value) != expected:
        raise ValidationError("registered assessment fields are unsupported")
    if value["schema_version"] != "registered_opportunity_assessment.v1":
        raise ValidationError("registered assessment schema is unsupported")
    if any(
        value[field] is not False
        for field in ("eligible_to_trade", "approval_created", "order_submitted")
    ):
        raise ValidationError("registered assessment contains trade authority")
    reasons = value["reason_codes"]
    if not isinstance(reasons, list):
        raise ValidationError("registered assessment reason_codes must be an array")
    return RegisteredOpportunityAssessment(
        assessment_id=value["assessment_id"],  # type: ignore[arg-type]
        asset_id=value["asset_id"],  # type: ignore[arg-type]
        instrument=value["instrument"],  # type: ignore[arg-type]
        created_at=_parse_instant(value["created_at"], "created_at"),
        expires_at=_parse_instant(value["expires_at"], "expires_at"),
        strategy_hash=value["strategy_hash"],  # type: ignore[arg-type]
        signal_hash=value["signal_hash"],  # type: ignore[arg-type]
        sentiment_hash=value["sentiment_hash"],  # type: ignore[arg-type]
        profitability_attestation_hash=value[
            "profitability_attestation_hash"
        ],  # type: ignore[arg-type]
        verdict=RegisteredVerdict(value["verdict"]),  # type: ignore[arg-type]
        reason_codes=tuple(reasons),
        reference_price=value["reference_price"],  # type: ignore[arg-type]
        stop_price=value["stop_price"],  # type: ignore[arg-type]
        target_price=value["target_price"],  # type: ignore[arg-type]
        eligible_for_risk_quote=value["eligible_for_risk_quote"],  # type: ignore[arg-type]
        eligible_to_trade=False,
        artifact_hash=value["artifact_hash"],  # type: ignore[arg-type]
    )


__all__ = (
    "ProfitabilityAttestation",
    "ProfitabilityAttestationStatus",
    "RegisteredOpportunityAssessment",
    "RegisteredVerdict",
    "build_registered_assessment",
    "registered_assessment_from_dict",
)
