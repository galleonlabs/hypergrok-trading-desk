"""Deterministic buy/sell/nothing assessment over versioned evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
from typing import Any

from .analysis import TechnicalBias, TechnicalSnapshot
from .canonical import canonical_decimal, canonical_json
from .errors import ValidationError
from .policy import exact_decimal
from .sentiment import CollectionMethod, SentimentLabel, SentimentSnapshot


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


def _artifact_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class AssessmentVerdict(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NOTHING = "nothing"
    UNAVAILABLE = "unavailable"


class ProfitabilityStatus(str, Enum):
    DRAFT = "draft"
    INCONCLUSIVE = "inconclusive"
    REJECTED = "rejected"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ProfitabilityGate:
    gate_id: str
    asset_id: str
    thesis_id: str
    thesis_version: str
    strategy_version: str
    artifact_hash: str
    status: ProfitabilityStatus
    issued_at: datetime
    expires_at: datetime
    oos_trades: int
    shadow_closed_signals: int
    net_expectancy_r: Decimal
    lower_confidence_bound_r: Decimal

    def __post_init__(self) -> None:
        for field in (
            "gate_id",
            "asset_id",
            "thesis_id",
            "thesis_version",
            "strategy_version",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(self, "artifact_hash", _sha256(self.artifact_hash, "artifact_hash"))
        if not isinstance(self.status, ProfitabilityStatus):
            try:
                object.__setattr__(self, "status", ProfitabilityStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid profitability status") from error
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValidationError("profitability gate must expire after issuance")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        for field in ("oos_trades", "shadow_closed_signals"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValidationError(f"{field} must be a non-negative integer")
        object.__setattr__(
            self,
            "net_expectancy_r",
            exact_decimal(self.net_expectancy_r, field="net_expectancy_r"),
        )
        object.__setattr__(
            self,
            "lower_confidence_bound_r",
            exact_decimal(
                self.lower_confidence_bound_r,
                field="lower_confidence_bound_r",
            ),
        )

    def is_active(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return (
            self.status is ProfitabilityStatus.QUALIFIED
            and self.issued_at <= checked < self.expires_at
            and self.net_expectancy_r > 0
            and self.lower_confidence_bound_r > 0
        )


@dataclass(frozen=True, slots=True)
class AssessmentPolicy:
    version: str = "assessment-v1"
    signal_ttl_seconds: int = 900

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version", maximum=64))
        if type(self.signal_ttl_seconds) is not int or self.signal_ttl_seconds <= 0:
            raise ValidationError("signal_ttl_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class OpportunityAssessment:
    assessment_id: str
    asset_id: str
    created_at: datetime
    expires_at: datetime
    policy_version: str
    technical_hash: str
    sentiment_hash: str
    profitability_gate_id: str | None
    verdict: AssessmentVerdict
    reason_codes: tuple[str, ...]
    eligible_for_risk_quote: bool
    eligible_to_trade: bool
    artifact_hash: str

    def is_fresh(self, at: datetime) -> bool:
        checked = _utc(at, "at")
        return self.created_at <= checked < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "opportunity_assessment.v1",
            "assessment_id": self.assessment_id,
            "asset_id": self.asset_id,
            "created_at": self.created_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "expires_at": self.expires_at.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "policy_version": self.policy_version,
            "technical_hash": self.technical_hash,
            "sentiment_hash": self.sentiment_hash,
            "profitability_gate_id": self.profitability_gate_id,
            "verdict": self.verdict.value,
            "reason_codes": list(self.reason_codes),
            "eligible_for_risk_quote": self.eligible_for_risk_quote,
            "eligible_to_trade": self.eligible_to_trade,
            "artifact_hash": self.artifact_hash,
            "approval_created": False,
            "order_submitted": False,
        }


def build_opportunity_assessment(
    *,
    assessment_id: str,
    asset_id: str,
    technical: TechnicalSnapshot,
    sentiment: SentimentSnapshot,
    profitability: ProfitabilityGate | None,
    at: datetime,
    policy: AssessmentPolicy = AssessmentPolicy(),
) -> OpportunityAssessment:
    if not isinstance(technical, TechnicalSnapshot):
        raise TypeError("technical must be TechnicalSnapshot")
    if not isinstance(sentiment, SentimentSnapshot):
        raise TypeError("sentiment must be SentimentSnapshot")
    if profitability is not None and not isinstance(profitability, ProfitabilityGate):
        raise TypeError("profitability must be ProfitabilityGate or None")
    if not isinstance(policy, AssessmentPolicy):
        raise TypeError("policy must be AssessmentPolicy")
    checked_id = _text(assessment_id, "assessment_id")
    checked_asset = _text(asset_id, "asset_id")
    if sentiment.asset_id != checked_asset:
        raise ValidationError("sentiment snapshot targets a different asset")
    checked_at = _utc(at, "at")
    technical_hash = _artifact_hash(technical.as_dict())
    reasons: list[str] = []
    verdict = AssessmentVerdict.NOTHING

    technical_expiry = technical.candle_close_time + timedelta(
        seconds=policy.signal_ttl_seconds
    )
    expires = min(technical_expiry, sentiment.expires_at)
    if not technical.candle_close_time <= checked_at < technical_expiry:
        verdict = AssessmentVerdict.UNAVAILABLE
        reasons.append("technical_signal_stale")
    elif not sentiment.is_fresh(checked_at):
        verdict = AssessmentVerdict.UNAVAILABLE
        reasons.append("sentiment_stale")
    elif not sentiment.available:
        verdict = AssessmentVerdict.UNAVAILABLE
        reasons.extend(sentiment.quality_reasons or ("sentiment_unavailable",))
    elif technical.bias is TechnicalBias.NOTHING:
        verdict = AssessmentVerdict.NOTHING
        reasons.append("technical_conditions_not_aligned")
    elif technical.bias is TechnicalBias.BUY and sentiment.label is SentimentLabel.BEARISH:
        verdict = AssessmentVerdict.NOTHING
        reasons.append("bearish_sentiment_veto")
    elif technical.bias is TechnicalBias.SELL and sentiment.label is SentimentLabel.BULLISH:
        verdict = AssessmentVerdict.NOTHING
        reasons.append("bullish_sentiment_veto")
    elif technical.bias is TechnicalBias.BUY:
        verdict = AssessmentVerdict.BUY
        reasons.append("technical_buy_sentiment_not_bearish")
    else:
        verdict = AssessmentVerdict.SELL
        reasons.append("technical_sell_sentiment_not_bullish")

    qualified = (
        profitability is not None
        and profitability.asset_id == checked_asset
        and profitability.strategy_version == technical.config_version
        and profitability.is_active(checked_at)
    )
    if profitability is None:
        reasons.append("profitability_gate_missing")
    elif profitability.asset_id != checked_asset:
        reasons.append("profitability_asset_mismatch")
    elif profitability.strategy_version != technical.config_version:
        reasons.append("profitability_strategy_mismatch")
    elif not profitability.is_active(checked_at):
        reasons.append("profitability_not_qualified")
    if sentiment.method is CollectionMethod.MANUAL_BROWSER:
        reasons.append("manual_sentiment_not_unattended")

    eligible_for_risk = (
        verdict in {AssessmentVerdict.BUY, AssessmentVerdict.SELL}
        and qualified
        and sentiment.eligible_for_unattended_use
        and checked_at < expires
    )
    if verdict in {AssessmentVerdict.BUY, AssessmentVerdict.SELL} and not eligible_for_risk:
        reasons.append("risk_quote_ineligible")

    payload = {
        "assessment_id": checked_id,
        "asset_id": checked_asset,
        "created_at": checked_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "policy_version": policy.version,
        "technical_hash": technical_hash,
        "sentiment_hash": sentiment.artifact_hash,
        "profitability_gate_id": None if profitability is None else profitability.gate_id,
        "verdict": verdict.value,
        "reason_codes": reasons,
        "eligible_for_risk_quote": eligible_for_risk,
        "eligible_to_trade": False,
    }
    return OpportunityAssessment(
        assessment_id=checked_id,
        asset_id=checked_asset,
        created_at=checked_at,
        expires_at=expires,
        policy_version=policy.version,
        technical_hash=technical_hash,
        sentiment_hash=sentiment.artifact_hash,
        profitability_gate_id=None if profitability is None else profitability.gate_id,
        verdict=verdict,
        reason_codes=tuple(reasons),
        eligible_for_risk_quote=eligible_for_risk,
        eligible_to_trade=False,
        artifact_hash=_artifact_hash(payload),
    )


__all__ = (
    "AssessmentPolicy",
    "AssessmentVerdict",
    "OpportunityAssessment",
    "ProfitabilityGate",
    "ProfitabilityStatus",
    "build_opportunity_assessment",
)
