import argparse
import json

import pytest

from hypergrok import cli

USER = "0x" + "1" * 40


def info_factory(*, value: str, abstraction: str, approval: int):
    def info(base: str, kind: str, **kwargs):
        del base, kwargs
        if kind == "allMids":
            return {"BTC": "100000", "ETH": "3000"}
        if kind == "clearinghouseState":
            return {"marginSummary": {"accountValue": value}}
        if kind == "userAbstraction":
            return abstraction
        if kind == "maxBuilderFee":
            return approval
        raise AssertionError(kind)

    return info


def test_doctor_separates_read_health_from_execution_readiness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_info", info_factory(value="0", abstraction="default", approval=0))
    cli._doctor(argparse.Namespace(user=None))
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "read-only-ready"
    assert report["markets_seen"] == 2
    assert not report["execution_ready"]
    assert not report["builder"]["eligible_by_balance"]
    assert report["builder"]["user_approval_sufficient"] is None


def test_doctor_never_tells_the_user_to_fund_the_builder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The builder treasury is Galleon's concern; users must never be pointed at it."""
    monkeypatch.setattr(cli, "_info", info_factory(value="0", abstraction="default", approval=0))
    cli._doctor(argparse.Namespace(user=None))
    report = json.loads(capsys.readouterr().out)
    guidance = report["next_action"].lower()
    assert "fund" not in guidance
    assert cli.GALLEON_BUILDER_ADDRESS.lower() not in guidance


def test_doctor_execution_readiness_ignores_builder_eligibility(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A user with their own setup complete is execution-ready even when the
    builder cannot currently collect a fee."""
    monkeypatch.setattr(cli, "_info", info_factory(value="0", abstraction="default", approval=0))
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", USER)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    cli._doctor(argparse.Namespace(user=USER))
    report = json.loads(capsys.readouterr().out)
    assert report["execution_ready"]
    assert not report["builder"]["eligible_by_balance"]
    assert report["builder"]["attribution_active"] is False


def test_doctor_reports_attribution_when_every_builder_gate_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_info", info_factory(value="100", abstraction="disabled", approval=10))
    monkeypatch.setenv("HYPERGROK_NETWORK", "mainnet")
    monkeypatch.setenv("HYPERGROK_ENABLE_MAINNET", "I_UNDERSTAND")
    monkeypatch.setenv("HYPERLIQUID_ACCOUNT_ADDRESS", USER)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unused")
    cli._doctor(argparse.Namespace(user=USER))
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "execution-ready"
    assert report["builder"]["standard_mode"]
    assert report["builder"]["user_approval_sufficient"]
    assert report["builder"]["attribution_active"]


def test_doctor_rejects_malformed_user_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_info", lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(cli.PlanError, match="hexadecimal"):
        cli._doctor(argparse.Namespace(user="not-an-address"))
