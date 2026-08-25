"""Deterministic adapters from saved analyses/tickets into the learning ledger."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .canonical import domain_hash
from .learning_ledger import (
    ApprovalReference,
    ApprovalState,
    ComponentVersions,
    DecisionClass,
    DecisionCycle,
    ExecutionReference,
    ExecutionState,
    LearningLedger,
    ProposedBracket,
    RecoveryExecutionReference,
    SourceEvidence,
)
from .execution_store import (
    CommandRecord,
    LegRecord,
    RecoveryCommand,
    TrustedApproval,
)
from .planning import risk_ticket_from_dict
from .research_store import AssetAnalysisRecord


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class LearningRecorder:
    """Record advisory analyses and exact staged proposals as separate cycles."""

    def __init__(self, ledger: LearningLedger) -> None:
        if not isinstance(ledger, LearningLedger):
            raise TypeError("ledger must be LearningLedger")
        self.ledger = ledger

    @staticmethod
    def analysis_cycle(record: AssetAnalysisRecord) -> DecisionCycle:
        payload = record.payload
        asset = payload["asset"]
        signal = payload["registered_signal"]
        assessment = payload["assessment"]
        sentiment = payload["sentiment"]
        technical = payload["descriptive_technical"]
        if not all(
            isinstance(item, Mapping)
            for item in (asset, signal, assessment, sentiment, technical)
        ):
            raise ValueError("analysis payload is incomplete")
        classification = DecisionClass(str(assessment["verdict"]))
        sources = [
            SourceEvidence(
                source_id=f"analysis:{record.analysis_hash}",
                source_kind="asset_analysis",
                content_hash=record.analysis_hash,
                observed_at=record.observed_at,
                captured_at=record.stored_at,
                source_version="asset_analysis.v1",
            ),
            SourceEvidence(
                source_id=f"history:{record.history_hash}",
                source_kind="completed_candle_history",
                content_hash=record.history_hash,
                observed_at=record.observed_at,
                captured_at=record.stored_at,
                source_version=str(asset.get("interval", "unknown")),
            ),
            SourceEvidence(
                source_id=f"signal:{record.signal_hash}",
                source_kind="registered_signal",
                content_hash=record.signal_hash,
                observed_at=_instant(signal["observed_at"]),
                captured_at=record.stored_at,
                source_version=f"{signal['strategy_id']}/{signal['strategy_version']}",
            ),
        ]
        if record.sentiment_hash is not None:
            snapshot = sentiment.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ValueError("analysis sentiment hash lacks snapshot")
            sources.append(
                SourceEvidence(
                    source_id=f"sentiment:{record.sentiment_hash}",
                    source_kind="sentiment",
                    content_hash=record.sentiment_hash,
                    observed_at=_instant(snapshot["collected_at"]),
                    captured_at=record.stored_at,
                    source_version=str(snapshot["classifier_version"]),
                )
            )
        bracket = None
        tags = ["advisory_analysis", "not_execution_authority"]
        if classification in {DecisionClass.BUY, DecisionClass.SELL}:
            entry = Decimal(str(assessment["reference_price"]))
            stop = Decimal(str(assessment["stop_price"]))
            target = Decimal(str(assessment["target_price"]))
            bracket = ProposedBracket(
                side=classification,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                quantity=Decimal("1"),
                risk_amount=abs(entry - stop),
                settlement_asset="USDC",
            )
            tags.append("unit_quantity_research_bracket")
        snapshot = sentiment.get("snapshot")
        sentiment_version = (
            "missing"
            if not isinstance(snapshot, Mapping)
            else str(snapshot.get("classifier_version", "unknown"))
        )
        assessment_hash = assessment.get("artifact_hash")
        thesis_hash = (
            record.analysis_hash
            if not isinstance(assessment_hash, str)
            else assessment_hash
        )
        return DecisionCycle(
            cycle_id=f"analysis-{record.analysis_hash[:32]}",
            asset=record.asset_id,
            instrument=str(signal["instrument"]),
            venue=str(asset["venue"]),
            environment=str(asset["execution_environment"]),
            timeframe=str(asset["interval"]),
            decided_at=record.observed_at,
            classification=classification,
            rationale_code=str(assessment["reason_codes"][0]),
            thesis_hash=thesis_hash,
            versions=ComponentVersions(
                strategy_version=str(signal["strategy_version"]),
                configuration_version=str(asset["config_hash"]),
                code_hash=str(signal["strategy_hash"]),
                decision_rule_version="registered-assessment/v1",
                ta_version=str(technical["config_version"]),
                sentiment_version=sentiment_version,
                risk_policy_version="unquoted",
            ),
            evidence=tuple(sources),
            bracket=bracket,
            tags=tuple(tags),
        )

    def record_analysis(self, record: AssetAnalysisRecord):
        cycle = self.analysis_cycle(record)
        event = self.ledger.record_decision(
            cycle,
            idempotency_key=f"analysis:{record.analysis_hash}",
        )
        return cycle, event

    def record_staged_ticket(self, payload: Mapping[str, Any]):
        if not isinstance(payload, Mapping) or payload.get("schema_version") != (
            "infrastructure_learning_ticket.v1"
        ):
            raise ValueError("staged learning ticket payload is invalid")
        if payload.get("profitability_qualified") is not False or payload.get(
            "mainnet_authorized"
        ) is not False:
            raise ValueError("staged learning ticket claims unsupported authority")
        ticket_document = payload.get("risk_ticket")
        if not isinstance(ticket_document, Mapping):
            raise ValueError("staged learning ticket lacks risk ticket")
        ticket = risk_ticket_from_dict(ticket_document)
        if ticket.plan is None:
            raise ValueError("staged learning ticket has no protected plan")
        plan = ticket.plan
        entry = plan.entry
        stop = plan.protective_stop
        target = plan.take_profit
        if entry.price_bound is None or stop.stop_price is None or target.stop_price is None:
            raise ValueError("staged learning bracket is incomplete")
        classification = DecisionClass(entry.side.value)
        analysis_hash = str(payload["analysis_hash"])
        grant_hash = str(payload["infrastructure_grant_hash"])
        loss_hash = str(payload["daily_loss_snapshot_hash"])
        evidence = (
            SourceEvidence(
                source_id=f"analysis:{analysis_hash}",
                source_kind="asset_analysis",
                content_hash=analysis_hash,
                observed_at=ticket.created_at,
                captured_at=ticket.created_at,
                source_version="asset_analysis.v1",
            ),
            SourceEvidence(
                source_id=f"grant:{grant_hash}",
                source_kind="infrastructure_learning_grant",
                content_hash=grant_hash,
                observed_at=ticket.created_at,
                captured_at=ticket.created_at,
                source_version="infrastructure_learning_grant.v1",
            ),
            SourceEvidence(
                source_id=f"daily-loss:{loss_hash}",
                source_kind="daily_loss_snapshot",
                content_hash=loss_hash,
                observed_at=ticket.created_at,
                captured_at=ticket.created_at,
                source_version="daily_loss_snapshot.v1",
            ),
        )
        cycle = DecisionCycle(
            cycle_id=f"trade-{ticket.ticket_hash[:32]}",
            asset=entry.instrument,
            instrument=entry.instrument,
            venue=entry.venue,
            environment=entry.environment.value,
            timeframe="4h",
            decided_at=ticket.created_at,
            classification=classification,
            rationale_code="infrastructure_learning_ticket_staged",
            thesis_hash=ticket.assessment_hash,
            versions=ComponentVersions(
                strategy_version=entry.strategy_version,
                configuration_version="infrastructure-learning/v1",
                code_hash=entry.code_hash,
                decision_rule_version="trusted-learning-quote/v1",
                ta_version="analysis-bound",
                sentiment_version=(
                    "manual-attended"
                    if payload.get("manual_sentiment_confirmation_required") is True
                    else "analysis-bound"
                ),
                risk_policy_version=ticket.policy_version,
            ),
            evidence=evidence,
            bracket=ProposedBracket(
                side=classification,
                entry_price=entry.price_bound,
                stop_price=stop.stop_price,
                target_price=target.stop_price,
                quantity=ticket.quantity,
                risk_amount=ticket.stressed_loss,
                settlement_asset="USDC",
            ),
            tags=("infrastructure_learning", "profitability_unqualified"),
        )
        event = self.ledger.record_decision(
            cycle,
            idempotency_key=f"ticket:{ticket.ticket_hash}",
        )
        return cycle, event

    def record_approval_reference(
        self,
        cycle_id: str,
        approval: TrustedApproval,
        *,
        state: ApprovalState,
        occurred_at: datetime,
    ):
        if not isinstance(approval, TrustedApproval):
            raise TypeError("approval must be TrustedApproval")
        reference = ApprovalReference(
            cycle_id=cycle_id,
            reference_id=approval.approval_id,
            state=state,
            occurred_at=occurred_at,
            ticket_hash=approval.ticket_hash,
            authority_kind="trusted_local_testnet_approval",
            authority_evidence_hash=approval.token_hash,
        )
        return self.ledger.record_approval(
            reference,
            idempotency_key=f"approval:{approval.approval_id}:{state.value}",
        )

    def record_execution_reference(
        self,
        cycle_id: str,
        command: CommandRecord,
        legs: tuple[LegRecord, ...],
        *,
        state: ExecutionState,
        occurred_at: datetime,
    ):
        if not isinstance(command, CommandRecord):
            raise TypeError("command must be CommandRecord")
        if len(legs) != 3 or any(not isinstance(item, LegRecord) for item in legs):
            raise TypeError("legs must contain the three durable command legs")
        record_hash = domain_hash(
            "trading-harness/learning-execution-reference/v1",
            {
                "command_id": command.command_id,
                "ticket_hash": command.ticket_hash,
                "plan_hash": command.plan_hash,
                "approval_id": command.approval_id,
                "state": command.state,
                "revision": command.revision,
                "leg_cloids": tuple(item.cloid for item in legs),
            },
        )
        reference = ExecutionReference(
            cycle_id=cycle_id,
            command_id=command.command_id,
            state=state,
            occurred_at=occurred_at,
            execution_record_hash=record_hash,
            client_order_ids=tuple(item.cloid for item in legs),
        )
        return self.ledger.record_execution(
            reference,
            idempotency_key=f"execution:{command.command_id}:{state.value}",
        )

    def record_recovery_execution_reference(
        self,
        cycle_id: str,
        parent: CommandRecord,
        recovery: RecoveryCommand,
        *,
        cloid: str,
    ):
        if not isinstance(parent, CommandRecord):
            raise TypeError("parent must be CommandRecord")
        if not isinstance(recovery, RecoveryCommand):
            raise TypeError("recovery must be RecoveryCommand")
        if (
            recovery.parent_command_id != parent.command_id
            or recovery.kind != "reduce_only_close"
        ):
            raise ValueError("recovery reference differs from parent close lifecycle")
        record_hash = domain_hash(
            "trading-harness/learning-recovery-execution-reference/v1",
            {
                "parent_command_id": parent.command_id,
                "recovery_command_id": recovery.recovery_command_id,
                "incident_id": recovery.incident_id,
                "recovery_hash": recovery.recovery_hash,
                "recovery_material_hash": recovery.recovery_material_hash,
                "kind": recovery.kind,
                "cloid": cloid,
            },
        )
        reference = RecoveryExecutionReference(
            cycle_id=cycle_id,
            parent_command_id=parent.command_id,
            recovery_command_id=recovery.recovery_command_id,
            kind=recovery.kind,
            occurred_at=recovery.created_at,
            recovery_record_hash=record_hash,
            client_order_ids=(cloid,),
        )
        return self.ledger.record_recovery_execution(
            reference,
            idempotency_key=f"recovery-execution:{recovery.recovery_command_id}",
        )


__all__ = ("LearningRecorder",)
