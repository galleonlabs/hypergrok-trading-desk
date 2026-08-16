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
