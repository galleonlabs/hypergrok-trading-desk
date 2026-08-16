import argparse
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from hypergrok import cli
from hypergrok.builder import BuilderError
from hypergrok.config import GALLEON_BUILDER_ADDRESS
from hypergrok.plans import OrderPlan, PlanError, load_plan, save_plan

ACCOUNT = "0x" + "1" * 40
API_WALLET = "0x" + "9" * 40


@pytest.fixture(autouse=True)
def isolated_execution_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPERGROK_STATE_DIR", str(tmp_path / "state"))


def make_plan(path: Path, network: str = "testnet") -> str:
    now = datetime.now(UTC)
    return save_plan(OrderPlan(
        schema_version=1, network=network, account=ACCOUNT, coin="BTC", side="buy",
        size="0.001", limit_px="100000", reduce_only=False, tif="Gtc",
        max_slippage_bps="30", builder_address=GALLEON_BUILDER_ADDRESS,
        builder_fee_tenths_bp=10, cloid="0x" + "a" * 32,
        created_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
    ), path)


class FakeExchange:
    sends: ClassVar[list[tuple[tuple, dict]]] = []
    response: ClassVar[dict] = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 123}}]}},
    }

    def __init__(self, wallet, base_url, account_address=None):
        self.wallet = wallet
        self.base_url = base_url
        self.account_address = account_address

    def set_expires_after(self, value):
        self.expires = value

    def order(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return self.response


class FakeCloid:
    @classmethod
    def from_str(cls, value):
        return value


def install_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    account = types.ModuleType("eth_account")
    account.Account = types.SimpleNamespace(from_key=lambda key: types.SimpleNamespace(address=API_WALLET))
    exchange = types.ModuleType("hyperliquid.exchange")
    exchange.Exchange = FakeExchange
    types_mod = types.ModuleType("hyperliquid.utils.types")
    types_mod.Cloid = FakeCloid
    monkeypatch.setitem(sys.modules, "eth_account", account)
    monkeypatch.setitem(sys.modules, "hyperliquid", types.ModuleType("hyperliquid"))
    monkeypatch.setitem(sys.modules, "hyperliquid.exchange", exchange)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils", types.ModuleType("hyperliquid.utils"))
    monkeypatch.setitem(sys.modules, "hyperliquid.utils.types", types_mod)


def live_info(kind, **kwargs):
    del kwargs
    if kind == "clearinghouseState":
        return {"marginSummary": {"accountValue": "100"}}
    if kind == "maxBuilderFee":
        return 10
    if kind == "userAbstraction":
        return "disabled"
    if kind in {"openOrders", "historicalOrders", "userFills"}:
        return []
    if kind == "allMids":
        return {"BTC": "100000"}
    if kind == "meta":
        return {"universe": [{"name": "BTC", "szDecimals": 5}]}
    if kind == "userRole":
        return {"role": "agent", "data": {"user": ACCOUNT}}
    raise AssertionError(kind)


def args(path: Path, digest: str):
    return argparse.Namespace(plan=str(path), confirm=digest, execute=True)


def test_exact_confirmation_is_required_before_sdk_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.json"
    make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    with pytest.raises(PlanError, match="Confirmation"):
        cli._execute(args(path, "wrong"))
    assert FakeExchange.sends == []


def test_builder_failure_means_zero_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "check_builder", lambda *a, **k: (_ for _ in ()).throw(BuilderError("no")))
    with pytest.raises(BuilderError):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_declared_account_must_match_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", "0x" + "2" * 40)
    with pytest.raises(PlanError, match="ACCOUNT_ADDRESS"):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_only_send_includes_builder_and_cloid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    cli._execute(args(path, digest))
    assert len(FakeExchange.sends) == 1
    call_args, call_kwargs = FakeExchange.sends[0]
    assert call_args[0] == "BTC"
    assert call_kwargs["cloid"] == "0x" + "a" * 32
    assert call_kwargs["builder"] == {"b": GALLEON_BUILDER_ADDRESS, "f": 10}


def test_duplicate_cloid_means_zero_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    monkeypatch.setattr(cli, "_seen_cloid", lambda *args: True)
    with pytest.raises(PlanError, match="already exists"):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_local_journal_blocks_concurrent_or_repeated_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    cli._execute(args(path, digest))
    with pytest.raises(RuntimeError, match="already has"):
        cli._execute(args(path, digest))
    assert len(FakeExchange.sends) == 1


def test_price_drift_means_zero_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)

    def drift_info(base, kind, **kwargs):
        del base
        if kind == "allMids":
            return {"BTC": "90000"}
        return live_info(kind, **kwargs)

    monkeypatch.setattr(cli, "_info", drift_info)
    with pytest.raises(PlanError, match="drift"):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_asset_precision_is_checked_before_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.json"
    make_plan(path)
    plan, _ = load_plan(path)
    too_precise = replace(plan, size="0.000001")
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    with pytest.raises(PlanError, match="permits 5"):
        cli._validate_asset_precision("https://example.invalid", too_precise)


def test_missing_key_means_zero_sends_after_live_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    with pytest.raises(cli.ConfigError, match="PRIVATE_KEY"):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_unauthorised_api_wallet_means_zero_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)

    def role_info(base, kind, **kwargs):
        del base
        if kind == "userRole":
            return {"role": "agent", "data": {"user": "0x" + "2" * 40}}
        return live_info(kind, **kwargs)

    monkeypatch.setattr(cli, "_info", role_info)
    with pytest.raises(PlanError, match="authorised API wallet"):
        cli._execute(args(path, digest))
    assert FakeExchange.sends == []


def test_mainnet_plan_uses_mainnet_endpoint_with_same_send_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeExchange.sends.clear()
    path = tmp_path / "p.json"
    digest = make_plan(path, network="mainnet")
    monkeypatch.setenv("HYPERGROK_NETWORK", "mainnet")
    monkeypatch.setenv("HYPERGROK_ENABLE_MAINNET", "I_UNDERSTAND")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    cli._execute(args(path, digest))
    assert len(FakeExchange.sends) == 1


def test_exchange_rejection_is_known_and_never_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeExchange.sends.clear()
    monkeypatch.setattr(
        FakeExchange,
        "response",
        {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "bad order"}]}}},
    )
    path = tmp_path / "p.json"
    digest = make_plan(path)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", ACCOUNT)
    install_sdk(monkeypatch)
    monkeypatch.setattr(cli, "_info", lambda base, kind, **kw: live_info(kind, **kw))
    with pytest.raises(PlanError, match="rejected"):
        cli._execute(args(path, digest))
    assert len(FakeExchange.sends) == 1
