"""Desk configuration with visible builder attribution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

GALLEON_BUILDER_ADDRESS = "0xC141Cbe4f4a9CAbc3cc78159a9268a4e008922CD"
GALLEON_BUILDER_FEE_TENTHS_BP = 10


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
    max_order_notional_usd: Decimal
    max_slippage_bps: Decimal
    state_dir: Path
    builder_address: str = GALLEON_BUILDER_ADDRESS
    builder_fee_tenths_bp: int = GALLEON_BUILDER_FEE_TENTHS_BP

    @classmethod
    def from_env(cls) -> DeskConfig:
        network = os.getenv("HYPERGROK_NETWORK", "testnet").lower()
        config = cls(
            network=network,
            mainnet_enabled=os.getenv("HYPERGROK_ENABLE_MAINNET") == "I_UNDERSTAND",
            max_order_notional_usd=_decimal("HYPERGROK_MAX_ORDER_NOTIONAL_USD", "1000"),
            max_slippage_bps=_decimal("HYPERGROK_MAX_SLIPPAGE_BPS", "30"),
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
        if self.max_order_notional_usd <= 0:
            raise ConfigError("Order notional cap must be positive")
        if not Decimal("0") < self.max_slippage_bps <= Decimal("100"):
            raise ConfigError("Slippage cap must be between 0 and 100 bps")
        if not self.state_dir.is_absolute():
            raise ConfigError("HYPERGROK_STATE_DIR must be an absolute path")
        if self.builder_address.lower() != GALLEON_BUILDER_ADDRESS.lower():
            raise ConfigError("The Galleon execution path cannot change builder attribution")
        if self.builder_fee_tenths_bp != GALLEON_BUILDER_FEE_TENTHS_BP:
            raise ConfigError("The Galleon builder fee must be 1 bp (f=10)")
