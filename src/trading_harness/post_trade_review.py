"""Deterministic descriptive reviews over the immutable learning ledger.

Reviews contain arithmetic summaries only.  They intentionally do not label a
strategy profitable, attribute causality, recommend a trade, or mutate a
strategy/configuration.  A different review algorithm must use a new explicit
version so historical reports remain reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Iterable

from .canonical import domain_hash
from .learning_ledger import DecisionClass, LearningLedger, LifecycleError


REVIEW_SCHEMA_VERSION = 1
REVIEW_ALGORITHM_VERSION = "deterministic-post-trade-review/v1"
REVIEW_HASH_DOMAIN = "trading-harness/post-trade-review/cycle/v1"
VERSION_IDENTITY_DOMAIN = "trading-harness/post-trade-review/version-identity/v1"
AGGREGATE_HASH_DOMAIN = "trading-harness/post-trade-review/aggregate/v1"
INTERPRETATION_BOUNDARY = (
    "descriptive_association_only_no_causality_or_future_profitability_claim"
)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise ValueError("ratio denominator must not be zero")
    with localcontext() as context:
        context.prec = 50
        return numerator / denominator


def _mean(values: Iterable[Decimal]) -> Decimal | None:
    items = tuple(values)
    if not items:
        return None
    return _ratio(_sum_exact(items), Decimal(len(items)))


def _median(values: Iterable[Decimal]) -> Decimal | None:
    items = sorted(values)
    if not items:
        return None
    middle = len(items) // 2
    if len(items) % 2:
        return items[middle]
    return _ratio(_add_exact(items[middle - 1], items[middle]), Decimal(2))


def _add_exact(*values: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 256
        return sum(values, Decimal(0))


def _subtract_exact(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 256
        return left - right


def _multiply_exact(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 256
        return left * right


def _sum_exact(values: Iterable[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 256
        return sum(values, Decimal(0))


def _duration_us(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class VersionIdentity:
    strategy_version: str
    configuration_version: str
    code_hash: str
    decision_rule_version: str
    ta_version: str
    sentiment_version: str
    risk_policy_version: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "fingerprint",
            domain_hash(
                VERSION_IDENTITY_DOMAIN,
                {
                    "strategy_version": self.strategy_version,
                    "configuration_version": self.configuration_version,
                    "code_hash": self.code_hash,
                    "decision_rule_version": self.decision_rule_version,
                    "ta_version": self.ta_version,
                    "sentiment_version": self.sentiment_version,
                    "risk_policy_version": self.risk_policy_version,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class CounterfactualReview:
    scenario_id: str
    scenario_version: str
    derivation_version: str
    path_hash: str
    market_source_snapshot_hash: str
    entry_rule: str
    entered: bool
    entry_bar_opened_at: str | None
    entry_bar_closed_at: str | None
    entry_time_precision: str | None
    outcome: str
    gross_r: Decimal
    assumed_cost_r_applied: Decimal
    net_r_after_assumed_cost: Decimal
    bars_evaluated: int
    exit_bar_opened_at: str | None
    exit_bar_closed_at: str | None
    exit_time_precision: str | None
    same_bar_conservative_stop: bool
    same_bar_entry_ambiguity: bool
    same_bar_target_ignored: bool


@dataclass(frozen=True, slots=True)
class CycleReview:
    cycle_id: str
    asset: str
    instrument: str
    decision: DecisionClass
    decided_at: datetime
    versions: VersionIdentity
    evidence_hashes: tuple[str, ...]
    planned_reward_risk: Decimal | None
    planned_risk_amount: Decimal | None
    settlement_asset: str | None
    approval_lifecycle: tuple[str, ...]
    execution_lifecycle: tuple[str, ...]
    fill_count: int
    entry_fill_count: int
    exit_fill_count: int
    entry_quantity: Decimal
    average_entry_price: Decimal | None
    total_fees: Decimal
    total_funding: Decimal
    venue_reported_closed_pnl: Decimal | None
    venue_reported_closed_pnl_asset: str | None
    quantity_weighted_slippage_bps: Decimal | None
    average_latency_us: Decimal | None
    decision_to_first_entry_us: int | None
    holding_duration_us: int | None
    mae_r: Decimal | None
    mfe_r: Decimal | None
    close_outcome_recorded: bool
    exit_reason: str | None
    gross_pnl: Decimal | None
    gross_pnl_evidence_event_hash: str | None
    net_pnl: Decimal | None
    realized_r_risk_basis_amount: Decimal | None
    realized_r: Decimal | None
    counterfactuals: tuple[CounterfactualReview, ...]
    data_quality_flags: tuple[str, ...]
    as_of_event_hash: str
    review_schema_version: int = REVIEW_SCHEMA_VERSION
    review_algorithm_version: str = REVIEW_ALGORITHM_VERSION
    interpretation_boundary: str = INTERPRETATION_BOUNDARY
    review_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "review_hash",
            domain_hash(
                REVIEW_HASH_DOMAIN,
                {
                    "cycle_id": self.cycle_id,
                    "asset": self.asset,
                    "instrument": self.instrument,
                    "decision": self.decision,
                    "decided_at": self.decided_at,
                    "versions": self.versions,
                    "evidence_hashes": self.evidence_hashes,
                    "planned_reward_risk": self.planned_reward_risk,
                    "planned_risk_amount": self.planned_risk_amount,
                    "settlement_asset": self.settlement_asset,
                    "approval_lifecycle": self.approval_lifecycle,
                    "execution_lifecycle": self.execution_lifecycle,
                    "fill_count": self.fill_count,
                    "entry_fill_count": self.entry_fill_count,
                    "exit_fill_count": self.exit_fill_count,
                    "entry_quantity": self.entry_quantity,
                    "average_entry_price": self.average_entry_price,
                    "total_fees": self.total_fees,
                    "total_funding": self.total_funding,
                    "venue_reported_closed_pnl": self.venue_reported_closed_pnl,
                    "venue_reported_closed_pnl_asset": self.venue_reported_closed_pnl_asset,
                    "quantity_weighted_slippage_bps": self.quantity_weighted_slippage_bps,
                    "average_latency_us": self.average_latency_us,
                    "decision_to_first_entry_us": self.decision_to_first_entry_us,
                    "holding_duration_us": self.holding_duration_us,
                    "mae_r": self.mae_r,
                    "mfe_r": self.mfe_r,
                    "close_outcome_recorded": self.close_outcome_recorded,
                    "exit_reason": self.exit_reason,
                    "gross_pnl": self.gross_pnl,
                    "gross_pnl_evidence_event_hash": self.gross_pnl_evidence_event_hash,
                    "net_pnl": self.net_pnl,
                    "realized_r_risk_basis_amount": self.realized_r_risk_basis_amount,
                    "realized_r": self.realized_r,
                    "counterfactuals": self.counterfactuals,
                    "data_quality_flags": self.data_quality_flags,
                    "as_of_event_hash": self.as_of_event_hash,
                    "review_schema_version": self.review_schema_version,
                    "review_algorithm_version": self.review_algorithm_version,
                    "interpretation_boundary": self.interpretation_boundary,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class AmountTotal:
    asset: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class CounterfactualAggregate:
    scenario_id: str
    scenario_version: str
    entry_rule: str
    derivation_version: str
    observation_count: int
    total_net_r_after_assumed_cost: Decimal
    mean_net_r_after_assumed_cost: Decimal


@dataclass(frozen=True, slots=True)
class VersionedReviewMetrics:
    versions: VersionIdentity
    as_of_event_hash: str
    as_of_recorded_at: datetime
    decision_cycle_count: int
    buy_count: int
    sell_count: int
    nothing_count: int
    unavailable_count: int
    approved_cycle_count: int
    executed_cycle_count: int
    closed_cycle_count: int
    realized_r_observation_count: int
    open_or_outcome_missing_count: int
    positive_realized_r_count: int
    negative_realized_r_count: int
    zero_realized_r_count: int
    total_realized_r: Decimal
    mean_realized_r: Decimal | None
    median_realized_r: Decimal | None
    positive_realized_r_share: Decimal | None
    total_fees_by_asset: tuple[AmountTotal, ...]
    total_funding_by_asset: tuple[AmountTotal, ...]
    total_net_pnl_by_asset: tuple[AmountTotal, ...]
    mean_mae_r: Decimal | None
    mean_mfe_r: Decimal | None
    mean_quantity_weighted_slippage_bps: Decimal | None
    mean_latency_us: Decimal | None
    mean_decision_to_first_entry_us: Decimal | None
    mean_holding_duration_us: Decimal | None
    counterfactuals: tuple[CounterfactualAggregate, ...]
    review_schema_version: int = REVIEW_SCHEMA_VERSION
    review_algorithm_version: str = REVIEW_ALGORITHM_VERSION
    interpretation_boundary: str = INTERPRETATION_BOUNDARY
    metrics_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics_hash",
            domain_hash(
                AGGREGATE_HASH_DOMAIN,
                {
                    "versions": self.versions,
                    "as_of_event_hash": self.as_of_event_hash,
                    "as_of_recorded_at": self.as_of_recorded_at,
                    "decision_cycle_count": self.decision_cycle_count,
                    "buy_count": self.buy_count,
                    "sell_count": self.sell_count,
                    "nothing_count": self.nothing_count,
                    "unavailable_count": self.unavailable_count,
                    "approved_cycle_count": self.approved_cycle_count,
                    "executed_cycle_count": self.executed_cycle_count,
                    "closed_cycle_count": self.closed_cycle_count,
                    "realized_r_observation_count": self.realized_r_observation_count,
                    "open_or_outcome_missing_count": self.open_or_outcome_missing_count,
                    "positive_realized_r_count": self.positive_realized_r_count,
                    "negative_realized_r_count": self.negative_realized_r_count,
                    "zero_realized_r_count": self.zero_realized_r_count,
                    "total_realized_r": self.total_realized_r,
                    "mean_realized_r": self.mean_realized_r,
                    "median_realized_r": self.median_realized_r,
                    "positive_realized_r_share": self.positive_realized_r_share,
                    "total_fees_by_asset": self.total_fees_by_asset,
                    "total_funding_by_asset": self.total_funding_by_asset,
                    "total_net_pnl_by_asset": self.total_net_pnl_by_asset,
                    "mean_mae_r": self.mean_mae_r,
                    "mean_mfe_r": self.mean_mfe_r,
                    "mean_quantity_weighted_slippage_bps": self.mean_quantity_weighted_slippage_bps,
                    "mean_latency_us": self.mean_latency_us,
                    "mean_decision_to_first_entry_us": self.mean_decision_to_first_entry_us,
                    "mean_holding_duration_us": self.mean_holding_duration_us,
                    "counterfactuals": self.counterfactuals,
                    "review_schema_version": self.review_schema_version,
                    "review_algorithm_version": self.review_algorithm_version,
                    "interpretation_boundary": self.interpretation_boundary,
                },
            ),
        )


class PostTradeReviewer:
    """Pure deterministic projection of a :class:`LearningLedger`."""

    def __init__(self, ledger: LearningLedger) -> None:
        if not isinstance(ledger, LearningLedger):
            raise TypeError("ledger must be LearningLedger")
        self.ledger = ledger

    @staticmethod
    def _versions(raw: dict[str, str]) -> VersionIdentity:
        return VersionIdentity(
            strategy_version=raw["strategy_version"],
            configuration_version=raw["configuration_version"],
            code_hash=raw["code_hash"],
            decision_rule_version=raw["decision_rule_version"],
            ta_version=raw["ta_version"],
            sentiment_version=raw["sentiment_version"],
            risk_policy_version=raw["risk_policy_version"],
        )

    def review_cycle(self, cycle_id: str) -> CycleReview:
        events = self.ledger.events(cycle_id=cycle_id)
        return self._review_events(events, requested_cycle_id=cycle_id)

    def _review_events(
        self,
        events: tuple,
        *,
        requested_cycle_id: str,
    ) -> CycleReview:
        if not events or events[0].event_type != "decision_cycle":
            raise LifecycleError(
                f"decision cycle {requested_cycle_id!r} does not exist"
            )
        cycle_id = events[0].cycle_id
        decision = events[0].payload
        classification = DecisionClass(decision["classification"])
        bracket = decision["bracket"]
        approvals = tuple(
            f"{item.payload['reference_id']}:{item.payload['state']}"
            for item in events
            if item.event_type == "approval_reference"
        )
        executions = tuple(
            f"{item.payload['command_id']}:{item.payload['state']}"
            for item in events
            if item.event_type == "execution_reference"
        )
        fill_events = [item for item in events if item.event_type == "venue_fill"]
        fills = [item.payload for item in fill_events]
        entry_fills = [item for item in fills if item["role"] == "entry"]
        exit_fills = [item for item in fills if item["role"] in ("exit", "protection")]
        entry_quantity = _sum_exact(Decimal(item["quantity"]) for item in entry_fills)
        average_entry = None
        if entry_quantity:
            average_entry = _ratio(
                _sum_exact(
                    _multiply_exact(
                        Decimal(item["price"]), Decimal(item["quantity"])
                    )
                    for item in entry_fills
                ),
                entry_quantity,
            )
        total_fees = _sum_exact(Decimal(item["fee"]) for item in fills)
        funding_events = [item.payload for item in events if item.event_type == "funding_payment"]
        total_funding = _sum_exact(
            Decimal(item["amount"]) for item in funding_events
        )
        fill_quantity = _sum_exact(Decimal(item["quantity"]) for item in fills)
        weighted_slippage = None
        venue_pnl_values = tuple(
            Decimal(item["venue_closed_pnl"])
            for item in fills
            if item.get("venue_closed_pnl") is not None
        )
        venue_pnl_assets = {
            item["venue_pnl_asset"]
            for item in fills
            if item.get("venue_pnl_asset") is not None
        }
        if len(venue_pnl_assets) > 1:
            raise LifecycleError("venue fill PnL assets disagree")
        venue_reported_closed_pnl = (
            None if not venue_pnl_values else _sum_exact(venue_pnl_values)
        )
        venue_reported_closed_pnl_asset = (
            None if not venue_pnl_assets else next(iter(venue_pnl_assets))
        )
        if fill_quantity:
            weighted_slippage = _ratio(
                _sum_exact(
                    _multiply_exact(
                        Decimal(item["slippage_bps"]), Decimal(item["quantity"])
                    )
                    for item in fills
                ),
                fill_quantity,
            )
        latency = _mean(Decimal(item["latency_us"]) for item in fills)
        close_event = next(
            (item for item in events if item.event_type == "close_outcome"), None
        )
        path_events = [item for item in events if item.event_type == "market_path"]
        entry_fill_hashes = tuple(
            item.event_hash
            for item in fill_events
            if item.payload["role"] == "entry"
        )
        eligible_paths = [
            item
            for item in path_events
            if item.payload["actual_excursion"] is not None
            and tuple(item.payload["entry_fill_event_hashes"]) == entry_fill_hashes
            and (
                close_event is None
                or item.payload["market_path"]["window_ended_at"]
                == close_event.payload["observation"]["completed_at"]
            )
        ]
        excursion = None
        excursion_path_ambiguous = False
        if close_event is not None:
            if len(eligible_paths) == 1:
                excursion = eligible_paths[0].payload["actual_excursion"]
            elif len(eligible_paths) > 1:
                excursion_path_ambiguous = True
        elif eligible_paths:
            excursion = eligible_paths[-1].payload["actual_excursion"]
        entry_event_times = tuple(
            item.occurred_at
            for item in fill_events
            if item.payload["role"] == "entry"
        )
        first_entry_at = min(entry_event_times) if entry_event_times else None
        decision_to_first_entry_us = (
            _duration_us(first_entry_at, events[0].occurred_at)
            if first_entry_at is not None
            else None
        )
        holding_duration_us = (
            _duration_us(close_event.occurred_at, first_entry_at)
            if close_event is not None and first_entry_at is not None
            else None
        )
        counterfactuals = tuple(
            CounterfactualReview(
                scenario_id=item.payload["spec"]["scenario_id"],
                scenario_version=item.payload["spec"]["scenario_version"],
                derivation_version=item.payload["derivation_version"],
                path_hash=item.payload["path_hash"],
                market_source_snapshot_hash=item.payload["market_source_snapshot_hash"],
                entry_rule=item.payload["spec"]["entry_rule"],
                entered=item.payload["entered"],
                entry_bar_opened_at=item.payload["entry_bar_opened_at"],
                entry_bar_closed_at=item.payload["entry_bar_closed_at"],
                entry_time_precision=item.payload["entry_time_precision"],
                outcome=item.payload["outcome"],
                gross_r=Decimal(item.payload["gross_r"]),
                assumed_cost_r_applied=Decimal(
                    item.payload["assumed_cost_r_applied"]
                ),
                net_r_after_assumed_cost=Decimal(
                    item.payload["net_r_after_assumed_cost"]
                ),
                bars_evaluated=item.payload["bars_evaluated"],
                exit_bar_opened_at=item.payload["exit_bar_opened_at"],
                exit_bar_closed_at=item.payload["exit_bar_closed_at"],
                exit_time_precision=item.payload["exit_time_precision"],
                same_bar_conservative_stop=item.payload[
                    "same_bar_conservative_stop"
                ],
                same_bar_entry_ambiguity=item.payload[
                    "same_bar_entry_ambiguity"
                ],
                same_bar_target_ignored=item.payload["same_bar_target_ignored"],
            )
            for item in events
            if item.event_type == "counterfactual"
        )
        flags: list[str] = []
        if executions and not funding_events:
            flags.append("funding_attribution_unverified")
        if venue_pnl_values and len(venue_pnl_values) != len(fills):
            flags.append("venue_closed_pnl_incomplete")
        exit_quantity = _sum_exact(
            Decimal(item["quantity"]) for item in exit_fills
        )
        net_pnl = None
        gross_pnl = None
        gross_pnl_evidence_event_hash = None
        realized_r_risk_basis_amount = None
        realized_r = None
        if classification in (DecisionClass.BUY, DecisionClass.SELL):
            if not approvals:
                flags.append("approval_reference_missing")
            if not executions:
                flags.append("execution_reference_missing")
            if executions and not entry_fills:
                flags.append("entry_fill_missing")
            if entry_fills and not path_events:
                flags.append("market_path_missing")
            if entry_fills and close_event is None:
                flags.append("position_open_or_outcome_missing")
            if close_event is not None and not exit_fills:
                flags.append("exit_fill_reference_missing")
            if bracket is not None and entry_quantity > Decimal(bracket["quantity"]):
                flags.append("planned_entry_quantity_exceeded")
            if close_event is not None:
                current_fill_basis = tuple(item.event_hash for item in fill_events)
                compatible_pnl_events = [
                    item
                    for item in events
                    if item.event_type == "close_pnl_correction"
                    and tuple(item.payload["gross_pnl_fill_basis_hashes"])
                    == current_fill_basis
                ]
                if (
                    tuple(close_event.payload["gross_pnl_fill_basis_hashes"])
                    == current_fill_basis
                ):
                    compatible_pnl_events.insert(0, close_event)
                pnl_evidence = (
                    compatible_pnl_events[-1] if compatible_pnl_events else None
                )
                if pnl_evidence is None:
                    flags.append("close_gross_pnl_basis_stale_or_unknown")
                else:
                    gross_pnl = Decimal(pnl_evidence.payload["observation"]["gross_pnl"])
                    gross_pnl_evidence_event_hash = pnl_evidence.event_hash
                    net_pnl = _add_exact(
                        _subtract_exact(gross_pnl, total_fees), total_funding
                    )
                if entry_quantity != exit_quantity:
                    flags.append("close_fill_quantity_not_flat")
                elif entry_quantity > 0 and net_pnl is not None:
                    realized_r_risk_basis_amount = _multiply_exact(
                        Decimal(bracket["risk_amount"]),
                        _ratio(entry_quantity, Decimal(bracket["quantity"])),
                    )
                    realized_r = _ratio(net_pnl, realized_r_risk_basis_amount)
                if not eligible_paths:
                    flags.append("exact_excursion_path_missing")
                elif excursion_path_ambiguous:
                    flags.append("excursion_path_ambiguous")

        return CycleReview(
            cycle_id=cycle_id,
            asset=decision["asset"],
            instrument=decision["instrument"],
            decision=classification,
            decided_at=events[0].occurred_at,
            versions=self._versions(decision["versions"]),
            evidence_hashes=tuple(item["content_hash"] for item in decision["evidence"]),
            planned_reward_risk=(
                Decimal(bracket["planned_reward_risk"]) if bracket is not None else None
            ),
            planned_risk_amount=(Decimal(bracket["risk_amount"]) if bracket is not None else None),
            settlement_asset=(bracket["settlement_asset"] if bracket is not None else None),
            approval_lifecycle=approvals,
            execution_lifecycle=executions,
            fill_count=len(fills),
            entry_fill_count=len(entry_fills),
            exit_fill_count=len(exit_fills),
            entry_quantity=entry_quantity,
            average_entry_price=average_entry,
            total_fees=total_fees,
            total_funding=total_funding,
            venue_reported_closed_pnl=venue_reported_closed_pnl,
            venue_reported_closed_pnl_asset=venue_reported_closed_pnl_asset,
            quantity_weighted_slippage_bps=weighted_slippage,
            average_latency_us=latency,
            decision_to_first_entry_us=decision_to_first_entry_us,
            holding_duration_us=holding_duration_us,
            mae_r=(Decimal(excursion["mae_r"]) if excursion is not None else None),
            mfe_r=(Decimal(excursion["mfe_r"]) if excursion is not None else None),
            close_outcome_recorded=close_event is not None,
            exit_reason=(
                close_event.payload["observation"]["exit_reason"]
                if close_event is not None
                else None
            ),
            gross_pnl=gross_pnl,
            gross_pnl_evidence_event_hash=gross_pnl_evidence_event_hash,
            net_pnl=net_pnl,
            realized_r_risk_basis_amount=realized_r_risk_basis_amount,
            realized_r=realized_r,
            counterfactuals=counterfactuals,
            data_quality_flags=tuple(sorted(flags)),
            as_of_event_hash=events[-1].event_hash,
        )

    def review_all(self) -> tuple[CycleReview, ...]:
        events = self.ledger.events()
        return self._reviews_from_snapshot(events)

    def _reviews_from_snapshot(self, events: tuple) -> tuple[CycleReview, ...]:
        cycle_ids = tuple(
            item.cycle_id for item in events if item.event_type == "decision_cycle"
        )
        return tuple(
            self._review_events(
                tuple(item for item in events if item.cycle_id == cycle_id),
                requested_cycle_id=cycle_id,
            )
            for cycle_id in cycle_ids
        )

    def aggregate_by_version(self) -> tuple[VersionedReviewMetrics, ...]:
        """Return descriptive metrics grouped by the complete version identity."""

        events = self.ledger.events()
        if not events:
            return ()
        reviews = self._reviews_from_snapshot(events)
        groups: dict[VersionIdentity, list[CycleReview]] = {}
        for review in reviews:
            groups.setdefault(review.versions, []).append(review)
        results: list[VersionedReviewMetrics] = []
        for versions, group in sorted(groups.items(), key=lambda item: item[0].fingerprint):
            realized = tuple(
                item.realized_r for item in group if item.realized_r is not None
            )
            closed = tuple(item for item in group if item.realized_r is not None)
            positive = sum(value > 0 for value in realized)
            negative = sum(value < 0 for value in realized)
            zero = sum(value == 0 for value in realized)
            fees = self._amounts_by_asset(
                (item.settlement_asset, item.total_fees)
                for item in group
                if item.settlement_asset is not None
            )
            funding = self._amounts_by_asset(
                (item.settlement_asset, item.total_funding)
                for item in group
                if item.settlement_asset is not None
            )
            net_pnl = self._amounts_by_asset(
                (item.settlement_asset, item.net_pnl)
                for item in closed
                if item.settlement_asset is not None and item.net_pnl is not None
            )
            counterfactual_groups: dict[tuple[str, str, str, str], list[Decimal]] = {}
            for review in group:
                for counterfactual in review.counterfactuals:
                    key = (
                        counterfactual.scenario_id,
                        counterfactual.scenario_version,
                        counterfactual.entry_rule,
                        counterfactual.derivation_version,
                    )
                    counterfactual_groups.setdefault(key, []).append(
                        counterfactual.net_r_after_assumed_cost
                    )
            counterfactuals = tuple(
                CounterfactualAggregate(
                    scenario_id=key[0],
                    scenario_version=key[1],
                    entry_rule=key[2],
                    derivation_version=key[3],
                    observation_count=len(values),
                    total_net_r_after_assumed_cost=_sum_exact(values),
                    mean_net_r_after_assumed_cost=_mean(values) or Decimal(0),
                )
                for key, values in sorted(counterfactual_groups.items())
            )
            results.append(
                VersionedReviewMetrics(
                    versions=versions,
                    as_of_event_hash=events[-1].event_hash,
                    as_of_recorded_at=events[-1].recorded_at,
                    decision_cycle_count=len(group),
                    buy_count=sum(item.decision is DecisionClass.BUY for item in group),
                    sell_count=sum(item.decision is DecisionClass.SELL for item in group),
                    nothing_count=sum(item.decision is DecisionClass.NOTHING for item in group),
                    unavailable_count=sum(
                        item.decision is DecisionClass.UNAVAILABLE for item in group
                    ),
                    approved_cycle_count=sum(
                        any(state.endswith(":approved") for state in item.approval_lifecycle)
                        for item in group
                    ),
                    executed_cycle_count=sum(item.entry_fill_count > 0 for item in group),
                    closed_cycle_count=sum(item.close_outcome_recorded for item in group),
                    realized_r_observation_count=len(closed),
                    open_or_outcome_missing_count=sum(
                        "position_open_or_outcome_missing" in item.data_quality_flags
                        for item in group
                    ),
                    positive_realized_r_count=positive,
                    negative_realized_r_count=negative,
                    zero_realized_r_count=zero,
                    total_realized_r=_sum_exact(realized),
                    mean_realized_r=_mean(realized),
                    median_realized_r=_median(realized),
                    positive_realized_r_share=(
                        _ratio(Decimal(positive), Decimal(len(realized)))
                        if realized
                        else None
                    ),
                    total_fees_by_asset=fees,
                    total_funding_by_asset=funding,
                    total_net_pnl_by_asset=net_pnl,
                    mean_mae_r=_mean(
                        item.mae_r for item in group if item.mae_r is not None
                    ),
                    mean_mfe_r=_mean(
                        item.mfe_r for item in group if item.mfe_r is not None
                    ),
                    mean_quantity_weighted_slippage_bps=_mean(
                        item.quantity_weighted_slippage_bps
                        for item in group
                        if item.quantity_weighted_slippage_bps is not None
                    ),
                    mean_latency_us=_mean(
                        item.average_latency_us
                        for item in group
                        if item.average_latency_us is not None
                    ),
                    mean_decision_to_first_entry_us=_mean(
                        Decimal(item.decision_to_first_entry_us)
                        for item in group
                        if item.decision_to_first_entry_us is not None
                    ),
                    mean_holding_duration_us=_mean(
                        Decimal(item.holding_duration_us)
                        for item in group
                        if item.holding_duration_us is not None
                    ),
                    counterfactuals=counterfactuals,
                )
            )
        return tuple(results)

    @staticmethod
    def _amounts_by_asset(
        values: Iterable[tuple[str | None, Decimal | None]],
    ) -> tuple[AmountTotal, ...]:
        totals: dict[str, Decimal] = {}
        for asset, amount in values:
            if asset is None or amount is None:
                continue
            totals[asset] = _add_exact(totals.get(asset, Decimal(0)), amount)
        return tuple(
            AmountTotal(asset=asset, amount=amount)
            for asset, amount in sorted(totals.items())
        )
