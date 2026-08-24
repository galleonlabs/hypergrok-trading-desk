"""Credential-free production preflight preparation for protected entries.

This adapter performs only allowlisted Hyperliquid ``/info`` reads.  It binds
fresh account, metadata, market depth and local loss-budget state into the
exact :class:`DispatchPackage` consumed by :class:`ExecutionDispatcher`.
Signing and transport remain separate isolated boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, TypeAlias

from .account_risk import AccountRiskLimits, compile_account_risk_snapshot
from .dispatch_preflight import build_dispatch_preflight
from .dispatcher import DispatchPackage
from .errors import StateConflict, ValidationError
from .execution_store import CommandRecord, ExecutionStore, RecoveryCommand
from .hyperliquid_account import HyperliquidAccountSnapshot, fetch_account_snapshot
from .hyperliquid_recovery import (
    NoopFenceAction,
    ReduceOnlyCloseAction,
    recovery_action_from_material,
)
from .hyperliquid_wire import HyperliquidNetwork, build_protected_order_action
from .market_data import get_market_brief
from .planning import ProtectedTradePlan, RiskSizingPolicy, RiskTicket
from .policy import decimal_subtract, exact_decimal
from .recovery_dispatcher import PreparedRecovery


Clock: TypeAlias = Callable[[], datetime]
AccountReader: TypeAlias = Callable[[str, str], HyperliquidAccountSnapshot]
MarketReader: TypeAlias = Callable[[str, str], Mapping[str, Any]]
DailyLossReader: TypeAlias = Callable[[datetime], Decimal | str | int]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


class TestnetEntryPreparer:
    """Fetch and bind send-time evidence for one already-approved command."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        main_account_address: str,
        limits: AccountRiskLimits,
        policy: RiskSizingPolicy,
        clock: Clock = _clock,
        account_reader: AccountReader | None = None,
        market_reader: MarketReader | None = None,
        daily_loss_reader: DailyLossReader | None = None,
    ) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be an exact ExecutionStore")
        if store.environment.value != "testnet":
            raise ValidationError("entry preparer is testnet-only")
        if not isinstance(limits, AccountRiskLimits):
            raise TypeError("limits must be AccountRiskLimits")
        if not isinstance(policy, RiskSizingPolicy):
            raise TypeError("policy must be RiskSizingPolicy")
        if (
            limits.environment.value != "testnet"
            or limits.account_id != store.account_id
            or limits.main_account_address != main_account_address
        ):
            raise ValidationError("risk limits differ from execution-store account")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if account_reader is not None and not callable(account_reader):
            raise TypeError("account_reader must be callable or None")
        if market_reader is not None and not callable(market_reader):
            raise TypeError("market_reader must be callable or None")
        if not callable(daily_loss_reader):
            raise TypeError(
                "daily_loss_reader is required; realized loss cannot default to zero"
            )
        self.store = store
        self.main_account_address = main_account_address
        self.limits = limits
        self.policy = policy
        self.clock = clock
        self.account_reader = account_reader or (
            lambda address, network: fetch_account_snapshot(
                address,
                network,
                clock=self.clock,
            )
        )
        self.market_reader = market_reader or (
            lambda symbol, network: get_market_brief(
                symbol,
                network,
                clock=self.clock,
            )
        )
        self.daily_loss_reader = daily_loss_reader

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception as error:
            raise ValidationError(
                f"entry preparer clock failed: {type(error).__name__}"
            ) from error
        return _utc(value, "entry preparer clock")

    def __call__(
        self,
        command: CommandRecord,
        ticket: RiskTicket,
        plan: ProtectedTradePlan,
        requested_at: datetime,
    ) -> DispatchPackage:
        if not isinstance(command, CommandRecord):
            raise TypeError("command must be CommandRecord")
        if not isinstance(ticket, RiskTicket) or ticket.plan != plan:
            raise TypeError("ticket must contain the exact ProtectedTradePlan")
        if not isinstance(plan, ProtectedTradePlan):
            raise TypeError("plan must be ProtectedTradePlan")
        started = _utc(requested_at, "requested_at")
        if (
            command.command_id == ""
            or command.ticket_hash != ticket.ticket_hash
            or command.plan_hash != plan.plan_hash
            or command.state != "claimed"
        ):
            raise StateConflict("entry preparer command binding or state differs")
        if (
            plan.entry.environment.value != "testnet"
            or plan.entry.account_id != self.store.account_id
            or plan.entry.venue != "hyperliquid"
        ):
            raise StateConflict("protected plan is outside the testnet account scope")
        instrument = plan.entry.instrument
        symbol = instrument.removesuffix("-PERP")
        venue = self.account_reader(self.main_account_address, "testnet")
        if not isinstance(venue, HyperliquidAccountSnapshot):
            raise TypeError("account_reader must return HyperliquidAccountSnapshot")
        market = self.market_reader(symbol, "testnet")
        if not isinstance(market, Mapping):
            raise TypeError("market_reader must return a mapping")
        checked_at = self._now()
        if checked_at < started:
            raise StateConflict("entry preparation clock moved backwards")
        daily_loss_used = exact_decimal(
            self.daily_loss_reader(checked_at),
            field="daily_loss_used",
        )
        if daily_loss_used < 0:
            raise ValidationError("daily_loss_used must be non-negative")
        reserved_loss, _ = self.store.get_reserved_exposure()
        existing_open_risk = decimal_subtract(
            reserved_loss,
            command.reserved_loss,
            field="pre-existing reserved risk",
        )
        if existing_open_risk < 0:
            raise StateConflict("command reservation exceeds account reservation")
        account = compile_account_risk_snapshot(
            venue,
            symbol=symbol,
            limits=self.limits,
            daily_loss_used=daily_loss_used,
            open_risk_used=existing_open_risk,
        )
        metadata = venue.metadata.instrument(symbol).to_wire_metadata()
        preflight = build_dispatch_preflight(
            command=command,
            ticket=ticket,
            account=account,
            venue_account=venue,
            metadata=metadata,
            market=market,
            policy=self.policy,
            at=checked_at,
        )
        protected_action = build_protected_order_action(
            plan,
            metadata,
            network=HyperliquidNetwork.TESTNET,
            at=checked_at,
        )
        return DispatchPackage(
            preflight=preflight,
            metadata=metadata,
            protected_action=protected_action,
        )


class TestnetRecoveryPreparer:
    """Reconstruct a queued recovery and refresh its bound source evidence."""

    def __init__(
        self,
        store: ExecutionStore,
        *,
        main_account_address: str,
        clock: Clock = _clock,
        account_reader: AccountReader | None = None,
    ) -> None:
        if type(store) is not ExecutionStore:
            raise TypeError("store must be an exact ExecutionStore")
        if store.environment.value != "testnet":
            raise ValidationError("recovery preparer is testnet-only")
        if not isinstance(main_account_address, str) or not main_account_address:
            raise ValidationError("main_account_address is required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if account_reader is not None and not callable(account_reader):
            raise TypeError("account_reader must be callable or None")
        self.store = store
        self.main_account_address = main_account_address
        self.clock = clock
        self.account_reader = account_reader or (
            lambda address, network: fetch_account_snapshot(
                address,
                network,
                clock=self.clock,
            )
        )

    def prepare(
        self,
        command: RecoveryCommand,
        *,
        at: datetime,
    ) -> PreparedRecovery:
        if not isinstance(command, RecoveryCommand):
            raise TypeError("command must be RecoveryCommand")
        if command != self.store.get_recovery_command(command.recovery_command_id):
            raise StateConflict("recovery command differs from durable state")
        checked_at = _utc(at, "at")
        try:
            material = json.loads(command.recovery_material_json)
        except (TypeError, ValueError) as error:
            raise StateConflict("durable recovery material is invalid") from error
        if not isinstance(material, dict):
            raise StateConflict("durable recovery material is not an object")
        action = recovery_action_from_material(material)
        if (
            action.recovery_hash != command.recovery_hash
            or action.account_id != self.store.account_id
            or action.main_account_address != self.main_account_address
            or action.incident_id != command.incident_id
            or action.network is not HyperliquidNetwork.TESTNET
        ):
            raise StateConflict("decoded recovery action differs from command scope")
        if checked_at.timestamp() * 1_000 >= action.expires_at_ms:
            raise StateConflict("queued recovery action is expired")
        if isinstance(action, NoopFenceAction):
            evidence = self.store.get_attempt(command.parent_command_id)
            if (
                evidence.state != "unknown"
                or evidence.attempt_id != action.attempt_id
                or evidence.nonce != action.original_nonce
                or evidence.preflight_hash != action.preflight_hash
                or evidence.signed_evidence_hash != action.signed_evidence_hash
                or evidence.transport_evidence_hash
                != action.transport_evidence_hash
            ):
                raise StateConflict("noop source attempt differs from durable action")
        else:
            evidence = self.account_reader(self.main_account_address, "testnet")
            if not isinstance(evidence, HyperliquidAccountSnapshot):
                raise TypeError("account_reader must return HyperliquidAccountSnapshot")
            expected_hash = (
                action.position_snapshot_hash
                if isinstance(action, ReduceOnlyCloseAction)
                else action.account_snapshot_hash
            )
            if (
                evidence.network != "testnet"
                or evidence.main_account_address != self.main_account_address
                or evidence.snapshot_hash != expected_hash
            ):
                raise StateConflict("fresh account snapshot differs from recovery source")
            age_ms = int(checked_at.timestamp() * 1_000) - evidence.server_time_ms
            if age_ms > 5_000 or age_ms < -5_000:
                raise StateConflict("recovery account source is stale")
        return PreparedRecovery(action=action, evidence=evidence)


__all__ = ("TestnetEntryPreparer", "TestnetRecoveryPreparer")
