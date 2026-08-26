"""Project durable execution facts into the immutable learning ledger.

This adapter has no credential, signer, transport, or venue-write capability.
It repairs the narrow crash window after attended admission, records command
lifecycle states, and projects fully reconciled parent-leg fills (including
fees, slippage, latency, and exact venue evidence) into the trade's learning
cycle.  Missing decision evidence blocks entry preparation instead of allowing
an unlearnable trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from typing import Any

from .canonical import domain_hash
from .errors import HarnessError, RecordNotFound, StateConflict, ValidationError
from .execution_store import (
    CommandRecord,
    ExecutionStore,
    RecoveryVenueFill,
    VenueFill as StoredFill,
)
from .learning_bridge import LearningRecorder
from .learning_ledger import (
    ApprovalState,
    DecisionClass,
    ExecutionState,
    FillRole,
    VenueFill,
)
from .planning import protected_trade_plan_from_dict


class LearningProjectionError(HarnessError):
    """Durable execution evidence could not be safely projected."""


@dataclass(frozen=True, slots=True)
class ExecutionLearningSyncReport:
    command_count: int
    projected_command_count: int
    execution_references_inserted: int
    fills_inserted: int
    fills_existing: int
    report_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "execution_learning_sync.v1",
            "command_count": self.command_count,
            "projected_command_count": self.projected_command_count,
            "execution_references_inserted": self.execution_references_inserted,
            "fills_inserted": self.fills_inserted,
            "fills_existing": self.fills_existing,
            "report_hash": self.report_hash,
        }


def _state_for(command: CommandRecord, store: ExecutionStore) -> ExecutionState | None:
    if command.state == "terminal":
        return ExecutionState.RECONCILED
    if command.state == "submitted_unknown":
        return ExecutionState.UNKNOWN
    if command.state == "reconciling":
        try:
            attempt = store.get_attempt(command.command_id)
        except RecordNotFound:
            return None
        return (
            ExecutionState.ACKNOWLEDGED
            if attempt.state == "response_received"
            else ExecutionState.UNKNOWN
        )
    if command.state == "claimed":
        try:
            store.get_attempt(command.command_id)
        except RecordNotFound:
            return None
        return ExecutionState.DISPATCHED
    return None


class ExecutionLearningProjector:
    """Idempotently mirror one-account execution records into learning."""

    def __init__(
        self,
        store: ExecutionStore,
        recorder: LearningRecorder,
        *,
        settlement_asset: str,
    ) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be exact ExecutionStore")
        if not isinstance(recorder, LearningRecorder):
            raise TypeError("recorder must be LearningRecorder")
        if (
            not isinstance(settlement_asset, str)
            or not settlement_asset
            or settlement_asset != settlement_asset.upper()
        ):
            raise ValidationError("settlement_asset is invalid")
        self.store = store
        self.recorder = recorder
        self.settlement_asset = settlement_asset

    @staticmethod
    def cycle_id(command: CommandRecord) -> str:
        return f"trade-{command.ticket_hash[:32]}"

    def _event_states(self, cycle_id: str, event_type: str) -> set[str]:
        return {
            str(event.payload["state"])
            for event in self.recorder.ledger.events(cycle_id=cycle_id)
            if event.event_type == event_type
        }

    def _ensure_references(self, command: CommandRecord) -> int:
        cycle_id = self.cycle_id(command)
        events = self.recorder.ledger.events(cycle_id=cycle_id)
        if not events or events[0].event_type != "decision_cycle":
            raise LearningProjectionError(
                "execution command has no immutable staged learning decision"
            )
        inserted = 0
        approval = self.store.get_approval(command.approval_id)
        approval_states = self._event_states(cycle_id, "approval_reference")
        if ApprovalState.APPROVED.value not in approval_states:
            self.recorder.record_approval_reference(
                cycle_id,
                approval,
                state=ApprovalState.APPROVED,
                occurred_at=approval.issued_at,
            )
            inserted += 1
        execution_states = self._event_states(cycle_id, "execution_reference")
        legs = self.store.get_legs(command.command_id)
        if ExecutionState.AUTHORIZED.value not in execution_states:
            self.recorder.record_execution_reference(
                cycle_id,
                command,
                legs,
                state=ExecutionState.AUTHORIZED,
                occurred_at=command.created_at,
            )
            inserted += 1
            execution_states.add(ExecutionState.AUTHORIZED.value)
        current = _state_for(command, self.store)
        if current is not None and current.value not in execution_states:
            self.recorder.record_execution_reference(
                cycle_id,
                command,
                legs,
                state=current,
                occurred_at=command.updated_at,
            )
            inserted += 1
        recovery_references = {
            str(event.payload["recovery_command_id"])
            for event in self.recorder.ledger.events(cycle_id=cycle_id)
            if event.event_type == "recovery_execution_reference"
        }
        for recovery in self.store.list_recovery_commands():
            if (
                recovery.parent_command_id != command.command_id
                or recovery.kind != "reduce_only_close"
                or recovery.recovery_command_id in recovery_references
            ):
                continue
            try:
                material = json.loads(recovery.recovery_material_json)
                cloid = material["cloid"]
            except (KeyError, TypeError, ValueError) as error:
                raise LearningProjectionError(
                    "recovery close lacks a durable client order ID"
                ) from error
            if not isinstance(cloid, str):
                raise LearningProjectionError(
                    "recovery close client order ID is invalid"
                )
            self.recorder.record_recovery_execution_reference(
                cycle_id,
                command,
                recovery,
                cloid=cloid,
            )
            inserted += 1
        return inserted

    def require_entry_ready(self, command_id: str) -> None:
        self.recorder.ledger.require_write_headroom()
        command = self.store.get_command(command_id)
        cycle_id = self.cycle_id(command)
        events = self.recorder.ledger.events(cycle_id=cycle_id)
        if not events or events[0].event_type != "decision_cycle":
            raise StateConflict("entry has no staged learning decision")
        approvals = {
            event.payload["state"]
            for event in events
            if event.event_type == "approval_reference"
        }
        executions = {
            (event.payload["command_id"], event.payload["state"])
            for event in events
            if event.event_type == "execution_reference"
        }
        if ApprovalState.APPROVED.value not in approvals or (
            command.command_id,
            ExecutionState.AUTHORIZED.value,
        ) not in executions:
            raise StateConflict("entry learning authorization evidence is incomplete")

    @staticmethod
    def _reference_price(plan: Any, role: str) -> Decimal:
        if role == "entry":
            value = plan.entry.price_bound
        elif role == "protective_stop":
            value = plan.protective_stop.stop_price
        else:
            value = plan.take_profit.stop_price
        if value is None:
            raise LearningProjectionError("protected plan lacks a fill reference price")
        return value

    def _project_fill(
        self,
        command: CommandRecord,
        fill: StoredFill,
    ):
        plan = protected_trade_plan_from_dict(
            self.store.get_plan_payload(command.plan_hash)
        )
        legs = {item.role: item for item in self.store.get_legs(command.command_id)}
        leg = legs.get(fill.role)
        if leg is None or leg.cloid != fill.cloid:
            raise LearningProjectionError("stored fill differs from its durable leg")
        role = (
            FillRole.ENTRY
            if fill.role == "entry"
            else (
                FillRole.PROTECTION
                if fill.role == "protective_stop"
                else FillRole.EXIT
            )
        )
        evidence_hash = domain_hash(
            "trading-harness/execution-learning-fill-evidence/v1",
            {
                "command_id": command.command_id,
                "ticket_hash": command.ticket_hash,
                "plan_hash": command.plan_hash,
                "fill": fill.as_dict(),
            },
        )
        projected = VenueFill(
            cycle_id=self.cycle_id(command),
            command_id=command.command_id,
            fill_id=fill.fill_id,
            order_id=(
                str(fill.venue_oid) if fill.venue_oid is not None else fill.cloid
            ),
            client_order_id=fill.cloid,
            role=role,
            side=DecisionClass(leg.side),
            venue_occurred_at=fill.occurred_at,
            observed_at=fill.observed_at or max(fill.occurred_at, command.updated_at),
            price=fill.price,
            reference_price=self._reference_price(plan, fill.role),
            quantity=fill.quantity,
            fee=fill.fee,
            fee_asset=fill.fee_token or self.settlement_asset,
            venue_evidence_hash=evidence_hash,
            venue_closed_pnl=fill.closed_pnl,
            venue_pnl_asset=(
                None if fill.closed_pnl is None else self.settlement_asset
            ),
        )
        before = len(
            tuple(
                event
                for event in self.recorder.ledger.events(
                    cycle_id=projected.cycle_id
                )
                if event.event_type == "venue_fill"
                and event.payload["fill_id"] == projected.fill_id
            )
        )
        event = self.recorder.ledger.record_fill(
            projected,
            idempotency_key=f"execution-fill:{command.command_id}:{fill.fill_id}",
        )
        return before == 0, event

    def _project_recovery_fill(
        self,
        command: CommandRecord,
        fill: RecoveryVenueFill,
    ):
        if fill.parent_command_id != command.command_id:
            raise LearningProjectionError(
                "recovery fill differs from its parent command"
            )
        recovery = self.store.get_recovery_command(fill.recovery_command_id)
        if (
            recovery.parent_command_id != command.command_id
            or recovery.kind != "reduce_only_close"
        ):
            raise LearningProjectionError(
                "recovery fill owner is not a durable reduce-only close"
            )
        try:
            material = json.loads(recovery.recovery_material_json)
            reference_price = Decimal(str(material["price_bound"]))
        except (KeyError, TypeError, ValueError) as error:
            raise LearningProjectionError(
                "recovery close lacks a reference price"
            ) from error
        evidence_hash = domain_hash(
            "trading-harness/execution-learning-recovery-fill-evidence/v1",
            {
                "command_id": command.command_id,
                "ticket_hash": command.ticket_hash,
                "plan_hash": command.plan_hash,
                "recovery_command_id": recovery.recovery_command_id,
                "recovery_hash": recovery.recovery_hash,
                "fill": fill.as_dict(),
            },
        )
        projected = VenueFill(
            cycle_id=self.cycle_id(command),
            command_id=command.command_id,
            fill_id=fill.fill_id,
            order_id=str(fill.venue_oid),
            client_order_id=fill.cloid,
            role=FillRole.EXIT,
            side=DecisionClass(fill.side),
            venue_occurred_at=fill.occurred_at,
            observed_at=fill.observed_at,
            price=fill.price,
            reference_price=reference_price,
            quantity=fill.quantity,
            fee=fill.fee,
            fee_asset=fill.fee_token,
            venue_evidence_hash=evidence_hash,
            venue_closed_pnl=fill.closed_pnl,
            venue_pnl_asset=self.settlement_asset,
        )
        before = len(
            tuple(
                event
                for event in self.recorder.ledger.events(
                    cycle_id=projected.cycle_id
                )
                if event.event_type == "venue_fill"
                and event.payload["fill_id"] == projected.fill_id
            )
        )
        event = self.recorder.ledger.record_fill(
            projected,
            idempotency_key=(
                f"execution-recovery-fill:{fill.recovery_command_id}:{fill.fill_id}"
            ),
        )
        return before == 0, event

    def synchronize(self) -> ExecutionLearningSyncReport:
        commands = self.store.list_commands()
        references = 0
        inserted_fills = 0
        existing_fills = 0
        projected = 0
        try:
            for command in commands:
                references += self._ensure_references(command)
                for fill in self.store.list_fills(command.command_id):
                    inserted, _ = self._project_fill(command, fill)
                    if inserted:
                        inserted_fills += 1
                    else:
                        existing_fills += 1
                for fill in self.store.list_recovery_fills(
                    parent_command_id=command.command_id
                ):
                    inserted, _ = self._project_recovery_fill(command, fill)
                    if inserted:
                        inserted_fills += 1
                    else:
                        existing_fills += 1
                projected += 1
        except (KeyError, TypeError, ValueError, StateConflict) as error:
            if isinstance(error, LearningProjectionError):
                raise
            raise LearningProjectionError(
                "execution learning projection failed closed"
            ) from error
        material = {
            "command_count": len(commands),
            "projected_command_count": projected,
            "execution_references_inserted": references,
            "fills_inserted": inserted_fills,
            "fills_existing": existing_fills,
        }
        return ExecutionLearningSyncReport(
            **material,
            report_hash=domain_hash(
                "trading-harness/execution-learning-sync/v1", material
            ),
        )


__all__ = (
    "ExecutionLearningProjector",
    "ExecutionLearningSyncReport",
    "LearningProjectionError",
)
