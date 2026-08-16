"""Desk configuration with visible builder attribution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

GALLEON_BUILDER_ADDRESS = "0xC141Cbe4f4a9CAbc3cc78159a9268a4e008922CD"
GALLEON_BUILDER_FEE_TENTHS_BP = 10

# HyperGrok imposes no risk ceiling of its own. Hyperliquid already publishes
# the real constraints per asset -- max leverage, tiered margin, lot precision,
# minimum order value -- and `hypergrok limits <COIN>` surfaces them. Sizing
# judgment belongs to the risk officer, not to a flat number in this file.
#
# The optional ceilings below are opt-in guardrails for anyone who wants the
# CLI itself to refuse a fat-fingered number. Unset means no ceiling.
#
# Correctness gates -- plan hash, account match, API-wallet role, duplicate
# cloid, tick precision, live price drift -- are not configurable and not
# preferences.
SANITY_MAX_RISK_PCT = Decimal("100")
SANITY_MAX_SLIPPAGE_BPS = Decimal("1000")
SANITY_MAX_PLAN_MINUTES = 1440

# Hyperliquid's own documented minimum order value, in USD.
HYPERLIQUID_MIN_ORDER_VALUE_USD = Decimal("10")


def _optional_decimal(name: str) -> Decimal | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not value.is_finite():
        raise ConfigError(f"{name} must be finite")
    return value


class ConfigError(ValueError):
    """Configuration is incomplete or unsafe."""


def _decimal(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except Exception as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not value.is_finite():
        raise ConfigError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class DeskConfig:
    network: str
    mainnet_enabled: bool
    # Optional opt-in guardrails. None means HyperGrok imposes no ceiling and
    # the risk officer, plus Hyperliquid's own margin engine, govern the size.
    max_order_notional_usd: Decimal | None
    max_risk_pct: Decimal | None
    # Not a ceiling: the live price-drift tolerance written into each plan, and
    # the mechanism that makes "you approved this exact order" mean something.
    max_slippage_bps: Decimal
    max_plan_minutes: int
    http_timeout_seconds: Decimal
    state_dir: Path
    builder_address: str = GALLEON_BUILDER_ADDRESS
    builder_fee_tenths_bp: int = GALLEON_BUILDER_FEE_TENTHS_BP

    @classmethod
    def from_env(cls) -> DeskConfig:
        network = os.getenv("HYPERGROK_NETWORK", "testnet").lower()
        try:
            plan_minutes = int(os.getenv("HYPERGROK_MAX_PLAN_MINUTES", "30"))
        except ValueError as exc:
            raise ConfigError("HYPERGROK_MAX_PLAN_MINUTES must be a whole number") from exc
        config = cls(
            network=network,
            mainnet_enabled=os.getenv("HYPERGROK_ENABLE_MAINNET") == "I_UNDERSTAND",
            max_order_notional_usd=_optional_decimal("HYPERGROK_MAX_ORDER_NOTIONAL_USD"),
            max_risk_pct=_optional_decimal("HYPERGROK_MAX_RISK_PCT"),
            max_slippage_bps=_decimal("HYPERGROK_MAX_SLIPPAGE_BPS", "30"),
            max_plan_minutes=plan_minutes,
            http_timeout_seconds=_decimal("HYPERGROK_HTTP_TIMEOUT_SECONDS", "15"),
            state_dir=Path(
                os.getenv("HYPERGROK_STATE_DIR", str(Path.home() / ".local/state/hypergrok/executions"))
            ).expanduser(),
        )
        config.validate()
        return config

    @property
    def api_url(self) -> str:
        if self.network == "mainnet":
            return "https://api.hyperliquid.xyz"
        return "https://api.hyperliquid-testnet.xyz"

    def validate(self) -> None:
        if self.network not in {"testnet", "mainnet"}:
            raise ConfigError("HYPERGROK_NETWORK must be testnet or mainnet")
        if self.network == "mainnet" and not self.mainnet_enabled:
            raise ConfigError("Mainnet is disabled; set HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND")
        if self.max_order_notional_usd is not None and self.max_order_notional_usd <= 0:
            raise ConfigError("HYPERGROK_MAX_ORDER_NOTIONAL_USD must be positive when set")
        if not Decimal("0") < self.max_slippage_bps <= SANITY_MAX_SLIPPAGE_BPS:
            raise ConfigError(
                f"HYPERGROK_MAX_SLIPPAGE_BPS must be above 0 and at most {SANITY_MAX_SLIPPAGE_BPS} bps"
            )
        if self.max_risk_pct is not None and not (
            Decimal("0") < self.max_risk_pct <= SANITY_MAX_RISK_PCT
        ):
            raise ConfigError(
                f"HYPERGROK_MAX_RISK_PCT must be above 0 and at most {SANITY_MAX_RISK_PCT} when set"
            )
        if not 1 <= self.max_plan_minutes <= SANITY_MAX_PLAN_MINUTES:
            raise ConfigError(
                f"HYPERGROK_MAX_PLAN_MINUTES must be between 1 and {SANITY_MAX_PLAN_MINUTES}"
            )
        if not Decimal("1") <= self.http_timeout_seconds <= Decimal("60"):
            raise ConfigError("HTTP timeout must be between 1 and 60 seconds")
        if not self.state_dir.is_absolute():
            raise ConfigError("HYPERGROK_STATE_DIR must be an absolute path")
        if self.builder_address.lower() != GALLEON_BUILDER_ADDRESS.lower():
            raise ConfigError("The Galleon execution path cannot change builder attribution")
        if self.builder_fee_tenths_bp != GALLEON_BUILDER_FEE_TENTHS_BP:
            raise ConfigError("The Galleon builder fee must be 1 bp (f=10)")
