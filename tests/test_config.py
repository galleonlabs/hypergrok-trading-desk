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
