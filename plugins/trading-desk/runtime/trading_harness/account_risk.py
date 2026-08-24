"""Compile a fresh read-only venue snapshot into bounded account risk inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import re

from .canonical import canonical_decimal, domain_hash
from .domain import Environment
from .errors import StateConflict, ValidationError
from .hyperliquid_account import HyperliquidAccountSnapshot
from .planning import AccountRiskSnapshot
from .policy import exact_decimal


_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_ZERO = Decimal("0")
_CONTEXT = Context(prec=96, rounding=ROUND_HALF_EVEN, Emin=-192, Emax=192)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field} must be non-empty trimmed text")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValidationError(f"{field} is invalid")
    return value


def _positive(value: object, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed <= _ZERO:
        raise ValidationError(f"{field} must be positive")
    return parsed


def _nonnegative(value: object, field: str) -> Decimal:
    parsed = exact_decimal(value, field=field)
    if parsed < _ZERO:
        raise ValidationError(f"{field} must be non-negative")
    return parsed


@dataclass(frozen=True, slots=True)
class AccountRiskLimits:
    account_id: str
    main_account_address: str
    environment: Environment
    daily_loss_limit: Decimal
    aggregate_open_risk_limit: Decimal
    max_notional: Decimal
    leverage: Decimal = Decimal("2")
    require_flat_account: bool = True
    version: str = "flat-account-canary-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "version", _text(self.version, "version", 64))
        if (
            not isinstance(self.main_account_address, str)
            or not _ADDRESS_RE.fullmatch(self.main_account_address)
        ):
            raise ValidationError("main_account_address must be a lowercase address")
        if not isinstance(self.environment, Environment):
            try:
                object.__setattr__(self, "environment", Environment(self.environment))
            except (TypeError, ValueError) as error:
                raise ValidationError("invalid account environment") from error
        if self.environment not in {Environment.TESTNET, Environment.MAINNET}:
            raise ValidationError("account risk limits require testnet or mainnet")
        for field in (
            "daily_loss_limit",
            "aggregate_open_risk_limit",
            "max_notional",
            "leverage",
        ):
            object.__setattr__(self, field, _positive(getattr(self, field), field))
        if self.leverage > Decimal("2"):
            raise ValidationError("initial account leverage cannot exceed 2")
        if self.require_flat_account is not True:
            raise ValidationError("v1 account compiler requires a dedicated flat account")

    @property
    def limits_hash(self) -> str:
        return domain_hash("trading-harness/account-risk-limits/v1", self)


def compile_account_risk_snapshot(
    venue: HyperliquidAccountSnapshot,
    *,
    symbol: str,
    limits: AccountRiskLimits,
    daily_loss_used: Decimal | str | int,
    open_risk_used: Decimal | str | int,
) -> AccountRiskSnapshot:
    """Bind venue truth and local budgets for one flat-account canary quote."""

    if not isinstance(venue, HyperliquidAccountSnapshot):
        raise TypeError("venue must be HyperliquidAccountSnapshot")
    if not isinstance(limits, AccountRiskLimits):
        raise TypeError("limits must be AccountRiskLimits")
    checked_symbol = _text(symbol, "symbol", 64)
    expected_network = limits.environment.value
    if venue.network != expected_network:
        raise StateConflict("account snapshot network does not match risk limits")
    if venue.main_account_address != limits.main_account_address:
        raise StateConflict("account snapshot address does not match risk limits")
    if venue.positions or venue.open_orders:
        raise StateConflict("v1 risk quoting requires a dedicated flat account")
    if (
        venue.margin_summary.total_notional_position != _ZERO
        or venue.margin_summary.total_margin_used != _ZERO
    ):
        raise StateConflict("flat account summary reports non-zero exposure")
    instrument = venue.metadata.instrument(checked_symbol)
    if instrument.is_delisted:
        raise StateConflict("delisted instrument cannot enter new risk")
    if limits.leverage > instrument.max_leverage:
        raise StateConflict("configured leverage exceeds fresh instrument metadata")
    used_daily = _nonnegative(daily_loss_used, "daily_loss_used")
    used_open = _nonnegative(open_risk_used, "open_risk_used")
    with localcontext(_CONTEXT) as context:
        daily_remaining = max(
            context.subtract(limits.daily_loss_limit, used_daily),
            _ZERO,
        )
        open_remaining = max(
            context.subtract(limits.aggregate_open_risk_limit, used_open),
            _ZERO,
        )
    equity = venue.margin_summary.account_value
    if equity <= _ZERO or venue.withdrawable <= _ZERO:
        raise StateConflict("account has no positive risk capital")
    available = min(equity, venue.withdrawable)
    with localcontext(_CONTEXT) as context:
        collateral_notional = context.multiply(available, limits.leverage)
        notional = min(limits.max_notional, collateral_notional)
        lot_size = context.scaleb(Decimal("1"), -instrument.sz_decimals)
    observed_at = _EPOCH + timedelta(milliseconds=venue.server_time_ms)
    received_at = _EPOCH + timedelta(milliseconds=venue.received_at_ms)
    material = {
        "venue_snapshot_hash": venue.snapshot_hash,
        "limits_hash": limits.limits_hash,
        "symbol": checked_symbol,
        "daily_loss_used": canonical_decimal(used_daily),
        "open_risk_used": canonical_decimal(used_open),
        "daily_loss_remaining": canonical_decimal(daily_remaining),
        "open_risk_remaining": canonical_decimal(open_remaining),
        "equity": canonical_decimal(equity),
        "available_collateral": canonical_decimal(available),
        "max_notional": canonical_decimal(notional),
        "lot_size": canonical_decimal(lot_size),
        "leverage": canonical_decimal(limits.leverage),
    }
    return AccountRiskSnapshot(
        account_id=limits.account_id,
        environment=limits.environment,
        observed_at=observed_at,
        received_at=received_at,
        equity=equity,
        available_collateral=available,
        daily_loss_remaining=daily_remaining,
        open_risk_remaining=open_remaining,
        max_notional=notional,
        lot_size=lot_size,
        leverage=limits.leverage,
        artifact_hash=domain_hash(
            "trading-harness/account-risk-snapshot/v1",
            material,
        ),
    )


__all__ = ("AccountRiskLimits", "compile_account_risk_snapshot")
