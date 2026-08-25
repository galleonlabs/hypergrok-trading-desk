"""Trusted public-read quote compiler for infrastructure TESTNET learning.

The service has no approval, execution-store, credential, signer, dispatcher,
transport, or venue-write capability.  It resolves one exact saved analysis,
reads a public account snapshot, applies a complete durable daily-loss budget,
and returns an opaque staging decision.  Profitability is explicitly false;
the ticket exists to collect disciplined learning evidence, not to claim edge.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeAlias

from .account_risk import AccountRiskLimits, compile_account_risk_snapshot
from .canonical import domain_hash
from .errors import RecordNotFound, StateConflict, ValidationError
from .execution_grant import SignedInfrastructureGrant, TrustedInfrastructureGrant
from .executor_config import ExecutorConfig
from .hyperliquid_account import HyperliquidAccountSnapshot, fetch_account_snapshot
from .planning import (
    PlanIdentity,
    RiskSizingPolicy,
    RiskTicketStatus,
    quote_infrastructure_learning_ticket,
)
from .registered_decision import registered_assessment_from_dict
from .research_store import ResearchStore
from .staging_inbox import TrustedQuoteDecision, TrustedQuoteRequest
from .strategy import CANDIDATE_V0


Clock: TypeAlias = Callable[[], datetime]
AccountReader: TypeAlias = Callable[[str, str], HyperliquidAccountSnapshot]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("quote clock must be timezone-aware")
    return value.astimezone(timezone.utc)


class InfrastructureLearningQuoteService:
    """Compile one non-authoritative learning ticket from installed policy."""

    def __init__(
        self,
        research_store: ResearchStore,
        *,
        config: ExecutorConfig,
        policy: RiskSizingPolicy,
        grant: SignedInfrastructureGrant | TrustedInfrastructureGrant,
        account_reader: AccountReader | None = None,
        clock: Clock = _clock,
    ) -> None:
        if not isinstance(research_store, ResearchStore):
            raise TypeError("research_store must be ResearchStore")
        if not isinstance(config, ExecutorConfig):
            raise TypeError("config must be ExecutorConfig")
        if not isinstance(policy, RiskSizingPolicy):
            raise TypeError("policy must be RiskSizingPolicy")
        if not isinstance(
            grant, (SignedInfrastructureGrant, TrustedInfrastructureGrant)
        ):
            raise TypeError("grant must be a signed or trusted infrastructure scope")
        if account_reader is not None and not callable(account_reader):
            raise TypeError("account_reader must be callable or None")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            config.environment.value != "testnet"
            or config.account_id != grant.account_id
            or config.risk_policy_hash != policy.policy_hash
            or grant.risk_policy_hash != policy.policy_hash
            or frozenset(config.allowed_instruments)
            != frozenset(grant.allowed_instruments)
            or config.max_reserved_loss > grant.max_loss
            or config.max_reserved_notional > grant.max_notional
            or config.max_leverage > grant.max_leverage
        ):
            raise ValidationError("quote service configuration differs from learning grant")
        self.research_store = research_store
        self.config = config
        self.policy = policy
        self.grant = grant
        self.clock = clock
        self.account_reader = account_reader or (
            lambda address, network: fetch_account_snapshot(
                address,
                network,
                clock=self.clock,
            )
        )

    def _now(self) -> datetime:
        try:
            return _utc(self.clock())
        except Exception as error:
            if isinstance(error, ValidationError):
                raise
            raise ValidationError("quote clock failed") from error

    @staticmethod
    def _blocked(code: str, analysis_hash: str | None = None) -> TrustedQuoteDecision:
        return TrustedQuoteDecision.blocked(
            block_code=code,
            analysis_hash=analysis_hash,
        )

    def __call__(self, request: TrustedQuoteRequest) -> TrustedQuoteDecision:
        if not isinstance(request, TrustedQuoteRequest):
            raise TypeError("request must be TrustedQuoteRequest")
        try:
            record = self.research_store.get_asset_analysis(
                request.expected_analysis_hash
            )
        except RecordNotFound:
            return self._blocked("analysis_not_found")
        if record.asset_id != request.asset_id:
            return self._blocked("analysis_asset_mismatch", record.analysis_hash)
        now = self._now()
        if now >= record.expires_at:
            return self._blocked("analysis_expired", record.analysis_hash)
        assessment_document = record.payload.get("assessment")
        if not isinstance(assessment_document, dict):
            return self._blocked("analysis_unavailable", record.analysis_hash)
        verdict = assessment_document.get("verdict")
        if verdict == "nothing":
            return self._blocked("nothing_to_trade", record.analysis_hash)
        if verdict != "buy" and verdict != "sell":
            return self._blocked("analysis_unavailable", record.analysis_hash)
        try:
            assessment = registered_assessment_from_dict(assessment_document)
        except (TypeError, ValueError):
            return self._blocked("assessment_invalid", record.analysis_hash)
        instrument = (
            assessment.instrument
            if assessment.instrument in self.config.allowed_instruments
            else f"{assessment.instrument}-PERP"
        )
        if instrument not in self.config.allowed_instruments:
            return self._blocked("instrument_not_allowed", record.analysis_hash)
        if not self.grant.is_active(now):
            return self._blocked("learning_grant_inactive", record.analysis_hash)
        deferred_loss_hash = domain_hash(
            "trading-harness/deferred-agent-daily-loss/v1",
            {
                "account_id": self.config.account_id,
                "config_hash": self.config.config_hash,
                "analysis_hash": record.analysis_hash,
                "quoted_at": now,
                "authoritative_recheck": "executor_preflight",
            },
        )
        try:
            venue = self.account_reader(self.config.main_account_address, "testnet")
            if not isinstance(venue, HyperliquidAccountSnapshot):
                raise TypeError("account reader returned wrong type")
            limits = AccountRiskLimits(
                account_id=self.config.account_id,
                main_account_address=self.config.main_account_address,
                environment=self.config.environment,
                daily_loss_limit=self.config.daily_loss_limit,
                aggregate_open_risk_limit=self.config.max_reserved_loss,
                max_notional=self.config.max_reserved_notional,
                leverage=self.config.max_leverage,
            )
            account = compile_account_risk_snapshot(
                venue,
                symbol=assessment.instrument.removesuffix("-PERP"),
                limits=limits,
                daily_loss_used=Decimal("0"),
                open_risk_used=Decimal("0"),
            )
            ticket = quote_infrastructure_learning_ticket(
                ticket_id=f"learn-{record.analysis_hash[:24]}",
                assessment=assessment,
                identity=PlanIdentity(
                    thesis_id=CANDIDATE_V0.strategy_id,
                    thesis_version="1",
                    strategy_version=CANDIDATE_V0.strategy_version,
                    venue="hyperliquid",
                    account_id=self.config.account_id,
                    environment=self.config.environment,
                    instrument=instrument,
                ),
                account=account,
                grant=self.grant,
                at=now,
                policy=self.policy,
                strategy=CANDIDATE_V0,
            )
        except (StateConflict, ValidationError, TypeError):
            return self._blocked("risk_quote_denied", record.analysis_hash)
        if ticket.status is not RiskTicketStatus.AWAITING_APPROVAL or ticket.plan is None:
            return self._blocked("risk_quote_denied", record.analysis_hash)
        sentiment = record.payload.get("sentiment")
        snapshot = sentiment.get("snapshot") if isinstance(sentiment, dict) else None
        manual = isinstance(snapshot, dict) and snapshot.get("method") == "manual_browser"
        return TrustedQuoteDecision.staged(
            analysis_hash=record.analysis_hash,
            ticket_payload={
                "schema_version": "infrastructure_learning_ticket.v1",
                "purpose": "infrastructure_learning",
                "profitability_qualified": False,
                "mainnet_authorized": False,
                "analysis_hash": record.analysis_hash,
                "analysis_record_hash": record.record_hash,
                "infrastructure_grant_hash": self.grant.grant_hash,
                "grant_authentication_deferred_to_control": isinstance(
                    self.grant, SignedInfrastructureGrant
                ),
                "daily_loss_snapshot_hash": deferred_loss_hash,
                "daily_loss_deferred_to_executor": True,
                "manual_sentiment_confirmation_required": manual,
                "risk_ticket": ticket.as_dict(),
            },
        )


__all__ = ("InfrastructureLearningQuoteService",)
