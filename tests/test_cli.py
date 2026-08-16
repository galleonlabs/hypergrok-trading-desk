import json

from hypergrok import cli
from hypergrok.cli import main


def test_invalid_size_input_is_a_clean_cli_error(capsys) -> None:
    code = main(
        [
            "size",
            "--equity",
            "not-a-number",
            "--entry",
            "100",
            "--stop",
            "90",
            "--risk-pct",
            "1",
            "--max-notional",
            "1000",
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "decimal numbers" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_plan_number_is_a_clean_cli_error(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HYPERGROK_STATE_DIR", str(tmp_path / "state"))
    code = main(
        [
            "plan-order",
            "--account",
            "0x" + "1" * 40,
            "--coin",
            "BTC",
            "--side",
            "buy",
            "--size",
            "not-a-number",
            "--limit-px",
            "100",
            "--out",
            str(tmp_path / "plan.json"),
        ]
    )
    assert code == 2
    captured = capsys.readouterr()
    assert "decimal numbers" in captured.err
    assert "Traceback" not in captured.err


def test_order_status_queries_exact_cloid_without_signing(capsys, monkeypatch) -> None:
    account = "0x" + "1" * 40
    cloid = "0x" + "a" * 32
    observed = {}

    def fake_info(base_url, kind, **kwargs):
        observed.update(base_url=base_url, kind=kind, kwargs=kwargs)
        return {"status": "order", "order": {"order": {"cloid": cloid}}}

    monkeypatch.setattr(cli, "_info", fake_info)
    assert main(["order-status", "--account", account, "--cloid", cloid]) == 0
    result = json.loads(capsys.readouterr().out)
    assert observed == {
        "base_url": "https://api.hyperliquid-testnet.xyz",
        "kind": "orderStatus",
        "kwargs": {"user": account, "oid": cloid},
    }
    assert result["status"]["status"] == "order"


def test_order_status_rejects_invalid_cloid_before_network(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_info", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    code = main(
        [
            "order-status",
            "--account",
            "0x" + "1" * 40,
            "--cloid",
            "not-a-cloid",
        ]
    )
    assert code == 2
    assert "128-bit" in capsys.readouterr().err
