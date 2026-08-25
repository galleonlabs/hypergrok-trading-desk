"""Fresh deterministic send-time checks for one already-admitted plan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any, Mapping

from .canonical import domain_hash
from .domain import Environment, Side
from .errors import AdmissionDenied, StateConflict, ValidationError
from .execution_store import CommandRecord, DispatchPreflight
from .hyperliquid_account import HyperliquidAccountSnapshot
from .hyperliquid_wire import PerpInstrumentMetadata
from .planning import AccountRiskSnapshot, RiskSizingPolicy, RiskTicket
from .policy import exact_decimal


_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN, Emin=-192, Emax=192)
_ZERO = Decimal("0")
_MAX_PREFLIGHT_AGE = timedelta(seconds=5)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field} must be an ISO-8601 string") from error
    return _utc(parsed, field)


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = exact_decimal(value, field=field)  # type: ignore[arg-type]
    except (TypeError, ValidationError) as error:
        raise ValidationError(f"{field} must be an exact decimal") from error
    if positive and parsed <= _ZERO:
        raise ValidationError(f"{field} must be positive")
    return parsed


def _market_hash(market: Mapping[str, Any]) -> str:
    return domain_hash("trading-harness/dispatch-market-snapshot/v1", market)


def _risk_policy_hash(policy: RiskSizingPolicy) -> str:
    return policy.policy_hash


def build_dispatch_preflight(
    *,
    command: CommandRecord,
    ticket: RiskTicket,
    account: AccountRiskSnapshot,
    venue_account: HyperliquidAccountSnapshot,
    metadata: PerpInstrumentMetadata,
    market: Mapping[str, Any],
    policy: RiskSizingPolicy,
    at: datetime,
    lifetime_seconds: int = 5,
) -> DispatchPreflight:
    """Revalidate immutable economics and fresh venue state or deny before signing."""

    if not isinstance(command, CommandRecord):
        raise TypeError("command must be CommandRecord")
    if not isinstance(ticket, RiskTicket) or ticket.plan is None:
        raise TypeError("ticket must contain a protected plan")
    if not isinstance(account, AccountRiskSnapshot):
        raise TypeError("account must be AccountRiskSnapshot")
    if not isinstance(venue_account, HyperliquidAccountSnapshot):
        raise TypeError("venue_account must be HyperliquidAccountSnapshot")
    if not isinstance(metadata, PerpInstrumentMetadata):
        raise TypeError("metadata must be PerpInstrumentMetadata")
    if not isinstance(market, Mapping):
        raise TypeError("market must be a mapping")
    if not isinstance(policy, RiskSizingPolicy):
        raise TypeError("policy must be RiskSizingPolicy")
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 30:
        raise ValidationError("lifetime_seconds must be from 1 to 30")
    checked_at = _utc(at, "at")
    plan = ticket.plan
    entry = plan.entry
    reasons: list[str] = []
    if command.ticket_hash != ticket.ticket_hash or command.plan_hash != plan.plan_hash:
        reasons.append("command_ticket_plan_mismatch")
    supplied_policy_hash = _risk_policy_hash(policy)
    if (
        ticket.policy_version != policy.version
        or ticket.policy_hash != supplied_policy_hash
    ):
        reasons.append("risk_policy_mismatch")
    if command.state != "claimed":
        reasons.append("command_not_claimed")
    if not ticket.created_at <= checked_at < ticket.expires_at:
        reasons.append("ticket_inactive")
    if checked_at >= entry.expires_at:
        reasons.append("plan_expired")
    if account.account_id != entry.account_id or account.environment is not entry.environment:
        reasons.append("account_scope_mismatch")
    if not account.is_fresh(
        checked_at,
        maximum_age_seconds=policy.account_max_age_seconds,
    ):
        reasons.append("account_snapshot_stale")
    if venue_account.network != entry.environment.value:
        reasons.append("venue_account_network_mismatch")
    venue_observed = _EPOCH + timedelta(milliseconds=venue_account.server_time_ms)
    venue_received = _EPOCH + timedelta(milliseconds=venue_account.received_at_ms)
    if (
        account.observed_at != venue_observed
        or account.received_at != venue_received
        or account.equity != venue_account.margin_summary.account_value
        or account.available_collateral
        != min(venue_account.margin_summary.account_value, venue_account.withdrawable)
    ):
        reasons.append("compiled_account_snapshot_mismatch")
    if venue_account.positions or venue_account.open_orders:
        reasons.append("account_not_flat")
    if (
        venue_account.margin_summary.total_notional_position != _ZERO
        or venue_account.margin_summary.total_margin_used != _ZERO
    ):
        reasons.append("account_summary_not_flat")
    if metadata.source_hash != venue_account.metadata.instrument(metadata.symbol).metadata_hash:
        reasons.append("metadata_snapshot_mismatch")
    if entry.instrument not in {metadata.symbol, f"{metadata.symbol}-PERP"}:
        reasons.append("instrument_metadata_mismatch")
    if metadata.is_delisted:
        reasons.append("instrument_delisted")
    if account.leverage > policy.max_leverage or account.leverage > metadata.max_leverage:
        reasons.append("leverage_limit")
    if entry.price_bound is None:
        reasons.append("entry_price_bound_missing")
        entry_notional = _ZERO
    else:
        with localcontext(_CONTEXT) as context:
            entry_notional = context.multiply(entry.quantity, entry.price_bound)
            equity_budget = context.multiply(account.equity, policy.risk_fraction)
        risk_budget = min(
            equity_budget,
            account.daily_loss_remaining,
            account.open_risk_remaining,
        )
        if ticket.stressed_loss > risk_budget:
            reasons.append("fresh_risk_budget_exceeded")
        if entry_notional > account.max_notional:
            reasons.append("fresh_notional_limit_exceeded")
    if (
        entry.environment is Environment.MAINNET
        and policy.risk_fraction > Decimal("0.001")
    ):
        reasons.append("mainnet_canary_risk_fraction_exceeded")

    network = market.get("network")
    symbol = market.get("symbol")
    if network != entry.environment.value or symbol != metadata.symbol:
        reasons.append("market_scope_mismatch")
    received = _parse_time(market.get("received_at"), "market.received_at")
    if not received <= checked_at < received + _MAX_PREFLIGHT_AGE:
        reasons.append("market_snapshot_stale")
    consistency = market.get("mid_consistency")
    if not isinstance(consistency, Mapping) or consistency.get("within_limit") is not True:
        reasons.append("market_mid_inconsistent")
    book = market.get("book")
    if not isinstance(book, Mapping):
        raise ValidationError("market.book must be an object")
    best_bid = _decimal(book.get("best_bid"), "market.best_bid", positive=True)
    best_ask = _decimal(book.get("best_ask"), "market.best_ask", positive=True)
    if best_bid > best_ask:
        reasons.append("crossed_market")
    depth = book.get("depth")
    band = depth.get("25bps") if isinstance(depth, Mapping) else None
    if not isinstance(band, Mapping):
        raise ValidationError("market 25bps depth must be an object")
    if entry.side is Side.BUY:
        visible = _decimal(band.get("ask_size"), "market.ask_size")
        complete = band.get("ask_complete") is True
        if entry.price_bound is not None and best_ask > entry.price_bound:
            reasons.append("entry_bound_not_crossable")
    else:
        visible = _decimal(band.get("bid_size"), "market.bid_size")
        complete = band.get("bid_complete") is True
        if entry.price_bound is not None and best_bid < entry.price_bound:
            reasons.append("entry_bound_not_crossable")
    if not complete:
        reasons.append("depth_completeness_unknown")
    if visible < entry.quantity:
        reasons.append("visible_depth_below_quantity")
    if reasons:
        raise AdmissionDenied(
            "DISPATCH_PREFLIGHT_DENIED",
            ",".join(sorted(set(reasons))),
        )

    account_expiry = account.received_at + _MAX_PREFLIGHT_AGE
    market_expiry = received + _MAX_PREFLIGHT_AGE
    expires = min(
        checked_at + timedelta(seconds=lifetime_seconds),
        account_expiry,
        market_expiry,
        ticket.expires_at,
        entry.expires_at,
    )
    if expires <= checked_at:
        raise AdmissionDenied(
            "DISPATCH_PREFLIGHT_STALE",
            "fresh evidence has no remaining preflight lifetime",
        )
    return DispatchPreflight(
        command_id=command.command_id,
        ticket_hash=ticket.ticket_hash,
        plan_hash=plan.plan_hash,
        environment=entry.environment,
        account_id=entry.account_id,
        account_snapshot_hash=account.artifact_hash,
        account_server_time_ms=venue_account.server_time_ms,
        metadata_hash=metadata.source_hash,
        market_snapshot_hash=_market_hash(dict(market)),
        risk_policy_hash=ticket.policy_hash,
        observed_at=checked_at,
        expires_at=expires,
        passed=True,
    )


__all__ = ("build_dispatch_preflight",)
