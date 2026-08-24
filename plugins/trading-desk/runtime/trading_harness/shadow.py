"""Prospective, append-only shadow profitability evidence.

Historical backtests cannot establish that a strategy continued to work after
registration.  This module records point-in-time shadow signals and their
costed outcomes in an immutable, hash-sealed event ledger.  It performs no
market reads, model inference, account access, authorization, or venue write.

Promotion requires *both* ninety elapsed calendar days and fifty closed,
eligible signals.  The primary prospective variant must have positive net-R
expectancy and a positive deterministic moving-block-bootstrap lower bound,
and its external drift assessment must pass.  Insufficient evidence is
``INCONCLUSIVE``; adequate but adverse evidence is ``REJECTED``.

TA-only and TA-plus-sentiment observations share a predeclared comparison ID.
Their paired net-R difference is evaluated prospectively.  Sentiment remains
``veto_only`` unless at least fifty closed pairs over ninety days have both a
positive mean incremental R and a positive lower confidence bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from enum import Enum
import re
from typing import Iterable, Sequence, cast

from .backtest import (
    BOOTSTRAP_SAMPLES,
    GateCheck,
    PerformanceMetrics,
    PromotionDecision,
    PromotionStatus,
    calculate_metrics,
)
from .canonical import canonical_data, domain_hash, validate_decimal_bounds
from .strategy import SignalDirection


SHADOW_PROTOCOL_HASH_DOMAIN = "trading-harness/shadow-protocol/v1"
SHADOW_SIGNAL_HASH_DOMAIN = "trading-harness/shadow-signal-record/v1"
SHADOW_OUTCOME_HASH_DOMAIN = "trading-harness/shadow-outcome-record/v1"
SHADOW_LEDGER_HASH_DOMAIN = "trading-harness/shadow-ledger/v1"
SHADOW_ARTIFACT_HASH_DOMAIN = "trading-harness/shadow-artifact/v1"
MINIMUM_SHADOW_ELAPSED = timedelta(days=90)
MINIMUM_CLOSED_SIGNALS = 50
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CALCULATION_CONTEXT = Context(
    prec=64,
    rounding=ROUND_HALF_EVEN,
    Emin=-192,
    Emax=192,
    capitals=1,
    clamp=0,
)


class ShadowLedgerError(ValueError):
    """An append would violate the prospective evidence ledger."""


class ShadowVariant(str, Enum):
    TA_ONLY = "ta_only"
    TA_SENTIMENT = "ta_plus_sentiment"


class ShadowRecordStatus(str, Enum):
    PENDING = "pending"
    CLOSED = "closed"
    INVALID = "invalid"


class DriftStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class SentimentAuthority(str, Enum):
    VETO_ONLY = "veto_only"
    DIRECTIONAL_ELIGIBLE = "directional_eligible"


def _text(value: object, field: str, maximum: int = 128) -> str:
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


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal, int, or exact string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
        validate_decimal_bounds(parsed, field=field)
    except (DecimalException, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a bounded finite decimal") from error
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be non-negative")
    return parsed


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_CALCULATION_CONTEXT) as context:
        return context.subtract(left, right)


def _elapsed_seconds(delta: timedelta) -> int:
    """Return exact floor seconds without a binary-float conversion."""

    return delta.days * 86_400 + delta.seconds


@dataclass(frozen=True, slots=True)
class ShadowProtocol:
    """Frozen prospective protocol registered before shadow observation."""

    protocol_id: str
    version: str
    asset_id: str
    registered_at: datetime
    started_at: datetime
    ta_strategy_hash: str
    sentiment_strategy_hash: str
    cost_model_hash: str
    drift_policy_hash: str
    minimum_elapsed_days: int = 90
    minimum_closed_signals: int = 50
    minimum_incremental_r: Decimal = Decimal("0")
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in ("protocol_id", "version", "asset_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "ta_strategy_hash",
            "sentiment_strategy_hash",
            "cost_model_hash",
            "drift_policy_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        registered = _instant(self.registered_at, "registered_at")
        started = _instant(self.started_at, "started_at")
        object.__setattr__(self, "registered_at", registered)
        object.__setattr__(self, "started_at", started)
        if started < registered:
            raise ValueError("shadow study cannot start before protocol registration")
        if type(self.minimum_elapsed_days) is not int or self.minimum_elapsed_days != 90:
            raise ValueError("minimum_elapsed_days is frozen at 90")
        if (
            type(self.minimum_closed_signals) is not int
            or self.minimum_closed_signals != 50
        ):
            raise ValueError("minimum_closed_signals is frozen at 50")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be integer 1")
        minimum_incremental = _decimal(
            self.minimum_incremental_r,
            "minimum_incremental_r",
            nonnegative=True,
        )
        object.__setattr__(self, "minimum_incremental_r", minimum_incremental)

    @property
    def protocol_hash(self) -> str:
        return domain_hash(SHADOW_PROTOCOL_HASH_DOMAIN, self)

    def strategy_hash_for(self, variant: ShadowVariant) -> str:
        return (
            self.ta_strategy_hash
            if variant is ShadowVariant.TA_ONLY
            else self.sentiment_strategy_hash
        )


@dataclass(frozen=True, slots=True)
class ShadowSignalRecord:
    """Point-in-time signal evidence appended before its expiry and outcome."""

    event_id: str
    signal_id: str
    comparison_id: str
    asset_id: str
    variant: ShadowVariant
    direction: SignalDirection
    strategy_hash: str
    signal_hash: str
    data_hash: str
    cost_model_hash: str
    evidence_hash: str
    observed_at: datetime
    expires_at: datetime
    recorded_at: datetime
    eligible: bool = True

    def __post_init__(self) -> None:
        for field in ("event_id", "signal_id", "comparison_id", "asset_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.variant, ShadowVariant):
            try:
                object.__setattr__(self, "variant", ShadowVariant(self.variant))
            except (TypeError, ValueError) as error:
                raise ValueError("variant is invalid") from error
        if not isinstance(self.direction, SignalDirection):
            try:
                object.__setattr__(self, "direction", SignalDirection(self.direction))
            except (TypeError, ValueError) as error:
                raise ValueError("direction is invalid") from error
        for field in (
            "strategy_hash",
            "signal_hash",
            "data_hash",
            "cost_model_hash",
            "evidence_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        observed = _instant(self.observed_at, "observed_at")
        expires = _instant(self.expires_at, "expires_at")
        recorded = _instant(self.recorded_at, "recorded_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "recorded_at", recorded)
        if expires <= observed:
            raise ValueError("signal expiry must follow observation")
        if not observed <= recorded <= expires:
            raise ValueError("signal must be recorded point-in-time before expiry")
        if type(self.eligible) is not bool:
            raise TypeError("eligible must be bool")

    @property
    def event_hash(self) -> str:
        return domain_hash(SHADOW_SIGNAL_HASH_DOMAIN, self)


@dataclass(frozen=True, slots=True)
class ShadowOutcomeRecord:
    """One immutable terminal event for a previously appended shadow signal."""

    event_id: str
    signal_id: str
    signal_event_hash: str
    strategy_hash: str
    signal_hash: str
    data_hash: str
    cost_model_hash: str
    outcome_evidence_hash: str
    status: ShadowRecordStatus
    closed_at: datetime
    recorded_at: datetime
    gross_r: Decimal | None = None
    cost_r: Decimal | None = None
    net_r: Decimal | None = None
    invalid_reason: str | None = None

    def __post_init__(self) -> None:
        for field in ("event_id", "signal_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "signal_event_hash",
            "strategy_hash",
            "signal_hash",
            "data_hash",
            "cost_model_hash",
            "outcome_evidence_hash",
        ):
            object.__setattr__(self, field, _hash(getattr(self, field), field))
        if not isinstance(self.status, ShadowRecordStatus):
            try:
                object.__setattr__(self, "status", ShadowRecordStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValueError("outcome status is invalid") from error
        if self.status is ShadowRecordStatus.PENDING:
            raise ValueError("an outcome event must be closed or invalid")
        closed = _instant(self.closed_at, "closed_at")
        recorded = _instant(self.recorded_at, "recorded_at")
        object.__setattr__(self, "closed_at", closed)
        object.__setattr__(self, "recorded_at", recorded)
        if recorded < closed:
            raise ValueError("outcome cannot be recorded before it closes")

        if self.status is ShadowRecordStatus.CLOSED:
            if self.invalid_reason is not None:
                raise ValueError("closed outcome cannot carry invalid_reason")
            if self.gross_r is None or self.cost_r is None or self.net_r is None:
                raise ValueError("closed outcome requires gross_r, cost_r, and net_r")
            gross = _decimal(self.gross_r, "gross_r")
            cost = _decimal(self.cost_r, "cost_r", nonnegative=True)
            net = _decimal(self.net_r, "net_r")
            if net != _subtract(gross, cost):
                raise ValueError("net_r must exactly equal gross_r minus cost_r")
            object.__setattr__(self, "gross_r", gross)
            object.__setattr__(self, "cost_r", cost)
            object.__setattr__(self, "net_r", net)
        else:
            if any(value is not None for value in (self.gross_r, self.cost_r, self.net_r)):
                raise ValueError("invalid outcome cannot carry performance values")
            object.__setattr__(
                self,
                "invalid_reason",
                _text(self.invalid_reason, "invalid_reason", 256),
            )

    @property
    def event_hash(self) -> str:
        return domain_hash(SHADOW_OUTCOME_HASH_DOMAIN, self)


ShadowEvent = ShadowSignalRecord | ShadowOutcomeRecord


def _ledger_hash(protocol_hash: str, events: Sequence[ShadowEvent]) -> str:
    previous = domain_hash(
        SHADOW_LEDGER_HASH_DOMAIN,
        {"protocol_hash": protocol_hash, "state": "empty"},
    )
    for sequence, event in enumerate(events):
        previous = domain_hash(
            SHADOW_LEDGER_HASH_DOMAIN,
            {
                "protocol_hash": protocol_hash,
                "sequence": sequence,
                "previous": previous,
                "event_hash": event.event_hash,
            },
        )
    return previous


@dataclass(frozen=True, slots=True)
class ShadowLedger:
    """Immutable event sequence with a verified hash chain."""

    protocol_hash: str
    events: tuple[ShadowEvent, ...]
    chain_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_hash", _hash(self.protocol_hash, "protocol_hash"))
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, (ShadowSignalRecord, ShadowOutcomeRecord))
            for event in self.events
        ):
            raise TypeError("events must be a tuple of shadow records")
        supplied = _hash(self.chain_hash, "chain_hash")
        if supplied != _ledger_hash(self.protocol_hash, self.events):
            raise ShadowLedgerError("shadow ledger hash chain does not match its events")

    @classmethod
    def create(cls, protocol: ShadowProtocol) -> "ShadowLedger":
        if not isinstance(protocol, ShadowProtocol):
            raise TypeError("protocol must be ShadowProtocol")
        return cls(
            protocol_hash=protocol.protocol_hash,
            events=(),
            chain_hash=_ledger_hash(protocol.protocol_hash, ()),
        )

    @classmethod
    def _trusted_append(
        cls,
        *,
        protocol_hash: str,
        events: tuple[ShadowEvent, ...],
        chain_hash: str,
    ) -> "ShadowLedger":
        """Construct after append invariants and the next chain link were checked."""

        value = object.__new__(cls)
        object.__setattr__(value, "protocol_hash", protocol_hash)
        object.__setattr__(value, "events", events)
        object.__setattr__(value, "chain_hash", chain_hash)
        return value

    def _append(self, event: ShadowEvent) -> "ShadowLedger":
        if self.events and event.recorded_at < self.events[-1].recorded_at:
            raise ShadowLedgerError("events must be appended in recorded-time order")
        events = self.events + (event,)
        chain_hash = domain_hash(
            SHADOW_LEDGER_HASH_DOMAIN,
            {
                "protocol_hash": self.protocol_hash,
                "sequence": len(self.events),
                "previous": self.chain_hash,
                "event_hash": event.event_hash,
            },
        )
        return self._trusted_append(
            protocol_hash=self.protocol_hash,
            events=events,
            chain_hash=chain_hash,
        )

    def append_signal(
        self, protocol: ShadowProtocol, signal: ShadowSignalRecord
    ) -> "ShadowLedger":
        if protocol.protocol_hash != self.protocol_hash:
            raise ShadowLedgerError("signal targets a different shadow protocol")
        if signal.asset_id != protocol.asset_id:
            raise ShadowLedgerError("signal asset does not match protocol")
        if signal.strategy_hash != protocol.strategy_hash_for(signal.variant):
            raise ShadowLedgerError("signal strategy hash does not match its variant")
        if signal.cost_model_hash != protocol.cost_model_hash:
            raise ShadowLedgerError("signal cost model was not preregistered")
        if signal.observed_at < protocol.started_at:
            raise ShadowLedgerError("signal predates the prospective shadow start")
        signals = tuple(
            event for event in self.events if isinstance(event, ShadowSignalRecord)
        )
        if any(event.event_id == signal.event_id for event in self.events):
            raise ShadowLedgerError("event_id is already present")
        if any(event.signal_id == signal.signal_id for event in signals):
            raise ShadowLedgerError("signal_id is already present")
        if any(event.signal_hash == signal.signal_hash for event in signals):
            raise ShadowLedgerError("signal_hash is already present")
        if any(
            event.comparison_id == signal.comparison_id
            and event.variant is signal.variant
            for event in signals
        ):
            raise ShadowLedgerError("comparison already has this strategy variant")
        return self._append(signal)

    def append_outcome(
        self, protocol: ShadowProtocol, outcome: ShadowOutcomeRecord
    ) -> "ShadowLedger":
        if protocol.protocol_hash != self.protocol_hash:
            raise ShadowLedgerError("outcome targets a different shadow protocol")
        if any(event.event_id == outcome.event_id for event in self.events):
            raise ShadowLedgerError("event_id is already present")
        signals = {
            event.signal_id: event
            for event in self.events
            if isinstance(event, ShadowSignalRecord)
        }
        signal = signals.get(outcome.signal_id)
        if signal is None:
            raise ShadowLedgerError("outcome has no prior signal")
        if any(
            isinstance(event, ShadowOutcomeRecord)
            and event.signal_id == outcome.signal_id
            for event in self.events
        ):
            raise ShadowLedgerError("signal already has a terminal outcome")
        exact_bindings = (
            (outcome.signal_event_hash, signal.event_hash),
            (outcome.strategy_hash, signal.strategy_hash),
            (outcome.signal_hash, signal.signal_hash),
            (outcome.data_hash, signal.data_hash),
            (outcome.cost_model_hash, signal.cost_model_hash),
        )
        if any(supplied != expected for supplied, expected in exact_bindings):
            raise ShadowLedgerError("outcome does not bind the exact signal evidence")
        if outcome.closed_at <= signal.observed_at:
            raise ShadowLedgerError("outcome must close after signal observation")
        if outcome.recorded_at < signal.recorded_at:
            raise ShadowLedgerError("outcome cannot predate signal registration")
        return self._append(outcome)

    def status_for(self, signal_id: str) -> ShadowRecordStatus:
        signal_id = _text(signal_id, "signal_id")
        if not any(
            isinstance(event, ShadowSignalRecord) and event.signal_id == signal_id
            for event in self.events
        ):
            raise ShadowLedgerError("unknown signal_id")
        for event in reversed(self.events):
            if isinstance(event, ShadowOutcomeRecord) and event.signal_id == signal_id:
                return event.status
        return ShadowRecordStatus.PENDING

    def verify_for(self, protocol: ShadowProtocol) -> None:
        """Replay every invariant so direct construction cannot bypass append APIs."""

        if not isinstance(protocol, ShadowProtocol):
            raise TypeError("protocol must be ShadowProtocol")
        replay = ShadowLedger.create(protocol)
        for event in self.events:
            replay = (
                replay.append_signal(protocol, event)
                if isinstance(event, ShadowSignalRecord)
                else replay.append_outcome(protocol, event)
            )
        if replay.chain_hash != self.chain_hash:
            raise ShadowLedgerError("replayed ledger does not match supplied chain")


@dataclass(frozen=True, slots=True)
class DriftAssessment:
    policy_hash: str
    status: DriftStatus
    assessed_at: datetime
    evidence_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_hash", _hash(self.policy_hash, "policy_hash"))
        object.__setattr__(self, "evidence_hash", _hash(self.evidence_hash, "evidence_hash"))
        if not isinstance(self.status, DriftStatus):
            try:
                object.__setattr__(self, "status", DriftStatus(self.status))
            except (TypeError, ValueError) as error:
                raise ValueError("drift status is invalid") from error
        object.__setattr__(
            self, "assessed_at", _instant(self.assessed_at, "assessed_at")
        )


@dataclass(frozen=True, slots=True)
class IncrementalComparison:
    paired_count: int
    ta_expectancy_r: Decimal
    sentiment_expectancy_r: Decimal
    mean_incremental_r: Decimal
    lower_95_incremental_r: Decimal | None
    promotion: PromotionDecision


@dataclass(frozen=True, slots=True)
class ShadowValidationArtifact:
    schema_version: int
    protocol_hash: str
    ledger_chain_hash: str
    as_of: datetime
    elapsed_seconds: int
    pending_signals: int
    invalid_signals: int
    ta_metrics: PerformanceMetrics
    sentiment_metrics: PerformanceMetrics
    drift: DriftAssessment
    promotion: PromotionDecision
    incremental: IncrementalComparison
    sentiment_authority: SentimentAuthority

    @property
    def artifact_hash(self) -> str:
        return domain_hash(SHADOW_ARTIFACT_HASH_DOMAIN, self)

    def to_dict(self) -> dict[str, object]:
        value = canonical_data(self)
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("canonical shadow artifact must be an object")
        return {**value, "artifact_hash": self.artifact_hash}


@dataclass(frozen=True, slots=True)
class _ReturnObservation:
    """Narrow adapter for backtest.calculate_metrics' net-R-only input use."""

    net_r: Decimal


def _metrics(outcomes: Sequence[ShadowOutcomeRecord]) -> PerformanceMetrics:
    return _metrics_from_returns(
        tuple(cast(Decimal, outcome.net_r) for outcome in outcomes)
    )


def _metrics_from_returns(
    returns: Sequence[Decimal], *, bootstrap_samples: int = BOOTSTRAP_SAMPLES
) -> PerformanceMetrics:
    observations = tuple(_ReturnObservation(value) for value in returns)
    # ``calculate_metrics`` currently consumes only the immutable ``net_r``
    # attribute.  Shadow does not fabricate prices or fill records merely to
    # reuse that safe, protocol-neutral calculation.
    return calculate_metrics(  # type: ignore[arg-type]
        cast(Iterable[object], observations),
        bootstrap_samples=bootstrap_samples,
    )


def _promotion(
    *,
    protocol: ShadowProtocol,
    as_of: datetime,
    metrics: PerformanceMetrics,
    drift: DriftAssessment,
    eligible_invalid_count: int,
) -> PromotionDecision:
    elapsed = as_of - protocol.started_at
    checks = (
        GateCheck(
            "minimum_elapsed_time",
            elapsed >= MINIMUM_SHADOW_ELAPSED,
            max(0, _elapsed_seconds(elapsed)),
            ">=7776000 seconds (90 days)",
        ),
        GateCheck(
            "minimum_closed_eligible_signals",
            metrics.trade_count >= MINIMUM_CLOSED_SIGNALS,
            metrics.trade_count,
            ">=50",
        ),
        GateCheck(
            "positive_costed_expectancy",
            metrics.expectancy_r > 0,
            metrics.expectancy_r,
            ">0 net R",
        ),
        GateCheck(
            "positive_bootstrap_lower_bound",
            metrics.bootstrap_lower_95_r is not None
            and metrics.bootstrap_lower_95_r > 0,
            metrics.bootstrap_lower_95_r,
            ">0 net R one-sided 95% lower bound",
        ),
        GateCheck(
            "drift_status",
            drift.status is DriftStatus.PASS,
            drift.status.value,
            "pass",
        ),
        GateCheck(
            "eligible_invalid_outcomes",
            eligible_invalid_count == 0,
            eligible_invalid_count,
            "=0",
        ),
    )
    inconclusive: list[str] = []
    if elapsed < MINIMUM_SHADOW_ELAPSED:
        inconclusive.append("fewer_than_90_elapsed_days")
    if metrics.trade_count < MINIMUM_CLOSED_SIGNALS:
        inconclusive.append("fewer_than_50_closed_eligible_signals")
    if drift.status is DriftStatus.UNKNOWN:
        inconclusive.append("drift_status_unknown")
    if eligible_invalid_count:
        inconclusive.append("eligible_signal_has_invalid_outcome")
    if inconclusive:
        return PromotionDecision(
            PromotionStatus.INCONCLUSIVE, checks, tuple(inconclusive)
        )
    failed = tuple(check.name for check in checks if not check.passed)
    if failed:
        return PromotionDecision(PromotionStatus.REJECTED, checks, failed)
    return PromotionDecision(PromotionStatus.PASS, checks, ())


def _incremental_comparison(
    *,
    protocol: ShadowProtocol,
    as_of: datetime,
    signals: Sequence[ShadowSignalRecord],
    outcomes: Sequence[ShadowOutcomeRecord],
) -> IncrementalComparison:
    outcome_by_signal = {outcome.signal_id: outcome for outcome in outcomes}
    pairs: dict[
        str,
        dict[ShadowVariant, tuple[ShadowSignalRecord, ShadowOutcomeRecord]],
    ] = {}
    for signal in signals:
        outcome = outcome_by_signal.get(signal.signal_id)
        if (
            not signal.eligible
            or outcome is None
            or outcome.status is not ShadowRecordStatus.CLOSED
        ):
            continue
        pairs.setdefault(signal.comparison_id, {})[signal.variant] = (signal, outcome)
    complete_pairs = tuple(
        pair
        for _, pair in sorted(
            pairs.items(),
            key=lambda item: (
                min(signal.observed_at for signal, _ in item[1].values()),
                item[0],
            ),
        )
        if ShadowVariant.TA_ONLY in pair and ShadowVariant.TA_SENTIMENT in pair
    )
    for pair in complete_pairs:
        ta_signal, ta_outcome = pair[ShadowVariant.TA_ONLY]
        sentiment_signal, sentiment_outcome = pair[ShadowVariant.TA_SENTIMENT]
        if ta_signal.observed_at != sentiment_signal.observed_at:
            raise ShadowLedgerError("paired variants have different observation times")
        if ta_outcome.closed_at != sentiment_outcome.closed_at:
            raise ShadowLedgerError("paired variants have different outcome horizons")
    ta_returns = tuple(
        cast(Decimal, pair[ShadowVariant.TA_ONLY][1].net_r)
        for pair in complete_pairs
    )
    sentiment_returns = tuple(
        cast(Decimal, pair[ShadowVariant.TA_SENTIMENT][1].net_r)
        for pair in complete_pairs
    )
    differences = tuple(
        _subtract(sentiment, ta)
        for sentiment, ta in zip(sentiment_returns, ta_returns)
    )
    count = len(differences)
    ta_expectancy = _metrics_from_returns(
        ta_returns, bootstrap_samples=0
    ).expectancy_r
    sentiment_expectancy = _metrics_from_returns(
        sentiment_returns, bootstrap_samples=0
    ).expectancy_r
    difference_metrics = _metrics_from_returns(differences)
    mean_incremental = difference_metrics.expectancy_r
    lower = difference_metrics.bootstrap_lower_95_r
    elapsed_ok = as_of - protocol.started_at >= MINIMUM_SHADOW_ELAPSED
    checks = (
        GateCheck("minimum_paired_signals", count >= 50, count, ">=50"),
        GateCheck(
            "minimum_elapsed_time",
            elapsed_ok,
            max(0, _elapsed_seconds(as_of - protocol.started_at)),
            ">=7776000 seconds (90 days)",
        ),
        GateCheck(
            "positive_mean_incremental_r",
            mean_incremental > protocol.minimum_incremental_r,
            mean_incremental,
            f">{protocol.minimum_incremental_r}R",
        ),
        GateCheck(
            "positive_incremental_lower_bound",
            lower is not None and lower > protocol.minimum_incremental_r,
            lower,
            f">{protocol.minimum_incremental_r}R one-sided 95% lower bound",
        ),
    )
    if count < 50 or not elapsed_ok:
        reasons = tuple(
            reason
            for condition, reason in (
                (count < 50, "fewer_than_50_closed_pairs"),
                (not elapsed_ok, "fewer_than_90_elapsed_days"),
            )
            if condition
        )
        decision = PromotionDecision(PromotionStatus.INCONCLUSIVE, checks, reasons)
    else:
        failed = tuple(check.name for check in checks if not check.passed)
        decision = (
            PromotionDecision(PromotionStatus.REJECTED, checks, failed)
            if failed
            else PromotionDecision(PromotionStatus.PASS, checks, ())
        )
    return IncrementalComparison(
        paired_count=count,
        ta_expectancy_r=ta_expectancy,
        sentiment_expectancy_r=sentiment_expectancy,
        mean_incremental_r=mean_incremental,
        lower_95_incremental_r=lower,
        promotion=decision,
    )


def evaluate_shadow(
    protocol: ShadowProtocol,
    ledger: ShadowLedger,
    drift: DriftAssessment,
    *,
    as_of: datetime,
) -> ShadowValidationArtifact:
    """Evaluate only evidence appended by ``as_of`` under the exact protocol."""

    if not isinstance(protocol, ShadowProtocol):
        raise TypeError("protocol must be ShadowProtocol")
    if not isinstance(ledger, ShadowLedger):
        raise TypeError("ledger must be ShadowLedger")
    if not isinstance(drift, DriftAssessment):
        raise TypeError("drift must be DriftAssessment")
    as_of = _instant(as_of, "as_of")
    if ledger.protocol_hash != protocol.protocol_hash:
        raise ShadowLedgerError("ledger targets a different protocol")
    ledger.verify_for(protocol)
    if as_of < protocol.started_at:
        raise ShadowLedgerError("as_of predates shadow start")
    if drift.policy_hash != protocol.drift_policy_hash:
        raise ShadowLedgerError("drift assessment uses a different policy")
    if drift.assessed_at < protocol.started_at:
        raise ShadowLedgerError("drift assessment predates the shadow study")
    if drift.assessed_at > as_of:
        raise ShadowLedgerError("drift assessment is future evidence")
    if any(event.recorded_at > as_of for event in ledger.events):
        raise ShadowLedgerError("ledger contains evidence recorded after as_of")

    signals = tuple(
        event for event in ledger.events if isinstance(event, ShadowSignalRecord)
    )
    outcomes = tuple(
        event for event in ledger.events if isinstance(event, ShadowOutcomeRecord)
    )
    signal_by_id = {signal.signal_id: signal for signal in signals}
    terminal_ids = {outcome.signal_id for outcome in outcomes}
    pending = sum(1 for signal in signals if signal.signal_id not in terminal_ids)
    invalid = sum(
        1 for outcome in outcomes if outcome.status is ShadowRecordStatus.INVALID
    )
    eligible_invalid = sum(
        1
        for outcome in outcomes
        if outcome.status is ShadowRecordStatus.INVALID
        and signal_by_id[outcome.signal_id].eligible
    )

    eligible_closed: dict[ShadowVariant, list[ShadowOutcomeRecord]] = {
        ShadowVariant.TA_ONLY: [],
        ShadowVariant.TA_SENTIMENT: [],
    }
    for outcome in outcomes:
        signal = signal_by_id[outcome.signal_id]
        if signal.eligible and outcome.status is ShadowRecordStatus.CLOSED:
            eligible_closed[signal.variant].append(outcome)
    ta_metrics = _metrics(eligible_closed[ShadowVariant.TA_ONLY])
    sentiment_metrics = _metrics(eligible_closed[ShadowVariant.TA_SENTIMENT])
    promotion = _promotion(
        protocol=protocol,
        as_of=as_of,
        metrics=sentiment_metrics,
        drift=drift,
        eligible_invalid_count=eligible_invalid,
    )
    incremental = _incremental_comparison(
        protocol=protocol,
        as_of=as_of,
        signals=signals,
        outcomes=outcomes,
    )
    authority = (
        SentimentAuthority.DIRECTIONAL_ELIGIBLE
        if promotion.status is PromotionStatus.PASS
        and incremental.promotion.status is PromotionStatus.PASS
        else SentimentAuthority.VETO_ONLY
    )
    return ShadowValidationArtifact(
        schema_version=1,
        protocol_hash=protocol.protocol_hash,
        ledger_chain_hash=ledger.chain_hash,
        as_of=as_of,
        elapsed_seconds=_elapsed_seconds(as_of - protocol.started_at),
        pending_signals=pending,
        invalid_signals=invalid,
        ta_metrics=ta_metrics,
        sentiment_metrics=sentiment_metrics,
        drift=drift,
        promotion=promotion,
        incremental=incremental,
        sentiment_authority=authority,
    )


__all__ = (
    "DriftAssessment",
    "DriftStatus",
    "IncrementalComparison",
    "SentimentAuthority",
    "ShadowLedger",
    "ShadowLedgerError",
    "ShadowOutcomeRecord",
    "ShadowProtocol",
    "ShadowRecordStatus",
    "ShadowSignalRecord",
    "ShadowValidationArtifact",
    "ShadowVariant",
    "evaluate_shadow",
)
