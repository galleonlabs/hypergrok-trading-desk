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
