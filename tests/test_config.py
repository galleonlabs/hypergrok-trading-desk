from decimal import Decimal

import pytest

from hypergrok.config import ConfigError, DeskConfig


def test_testnet_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HYPERGROK_NETWORK", "HYPERGROK_ENABLE_MAINNET"):
        monkeypatch.delenv(name, raising=False)
    assert DeskConfig.from_env().network == "testnet"


def test_mainnet_requires_literal_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_NETWORK", "mainnet")
    monkeypatch.delenv("HYPERGROK_ENABLE_MAINNET", raising=False)
    with pytest.raises(ConfigError, match="disabled"):
        DeskConfig.from_env()


def test_both_official_network_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_NETWORK", "testnet")
    assert DeskConfig.from_env().api_url == "https://api.hyperliquid-testnet.xyz"
    monkeypatch.setenv("HYPERGROK_NETWORK", "mainnet")
    monkeypatch.setenv("HYPERGROK_ENABLE_MAINNET", "I_UNDERSTAND")
    config = DeskConfig.from_env()
    assert config.mainnet_enabled
    assert config.api_url == "https://api.hyperliquid.xyz"


def test_state_directory_must_be_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_STATE_DIR", "relative-state")
    with pytest.raises(ConfigError, match="absolute"):
        DeskConfig.from_env()


def test_http_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_HTTP_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError, match="timeout"):
        DeskConfig.from_env()
    monkeypatch.setenv("HYPERGROK_HTTP_TIMEOUT_SECONDS", "61")
    with pytest.raises(ConfigError, match="timeout"):
        DeskConfig.from_env()
    monkeypatch.setenv("HYPERGROK_HTTP_TIMEOUT_SECONDS", "12.5")
    assert DeskConfig.from_env().http_timeout_seconds == Decimal("12.5")


def test_risk_limits_are_user_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_MAX_RISK_PCT", "7.5")
    monkeypatch.setenv("HYPERGROK_MAX_SLIPPAGE_BPS", "250")
    monkeypatch.setenv("HYPERGROK_MAX_PLAN_MINUTES", "120")
    config = DeskConfig.from_env()
    assert config.max_risk_pct == Decimal("7.5")
    assert config.max_slippage_bps == Decimal("250")
    assert config.max_plan_minutes == 120


def test_no_risk_ceiling_is_imposed_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Risk appetite is the user's, and the exchange's, not HyperGrok's."""
    for name in ("HYPERGROK_MAX_RISK_PCT", "HYPERGROK_MAX_ORDER_NOTIONAL_USD"):
        monkeypatch.delenv(name, raising=False)
    config = DeskConfig.from_env()
    assert config.max_risk_pct is None
    assert config.max_order_notional_usd is None


def test_drift_tolerance_always_has_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Price drift is a correctness protection, not a risk preference."""
    monkeypatch.delenv("HYPERGROK_MAX_SLIPPAGE_BPS", raising=False)
    assert DeskConfig.from_env().max_slippage_bps == Decimal("30")


def test_an_empty_value_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_MAX_RISK_PCT", "")
    assert DeskConfig.from_env().max_risk_pct is None


def test_sanity_bounds_reject_certainly_wrong_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value, match in [
        ("HYPERGROK_MAX_RISK_PCT", "0", "MAX_RISK_PCT"),
        ("HYPERGROK_MAX_ORDER_NOTIONAL_USD", "-5", "NOTIONAL"),
        ("HYPERGROK_MAX_RISK_PCT", "101", "MAX_RISK_PCT"),
        ("HYPERGROK_MAX_SLIPPAGE_BPS", "5000", "SLIPPAGE"),
        ("HYPERGROK_MAX_PLAN_MINUTES", "0", "PLAN_MINUTES"),
        ("HYPERGROK_MAX_PLAN_MINUTES", "99999", "PLAN_MINUTES"),
    ]:
        monkeypatch.setenv(name, value)
        with pytest.raises(ConfigError, match=match):
            DeskConfig.from_env()
        monkeypatch.delenv(name)
