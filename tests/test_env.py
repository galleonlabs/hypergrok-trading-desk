import os
from pathlib import Path

import pytest

from hypergrok.env import find_dotenv, load_dotenv, parse_dotenv


def test_parses_comments_blanks_quotes_and_export() -> None:
    parsed = parse_dotenv(
        "\n".join(
            [
                "# a comment",
                "",
                "HYPERGROK_NETWORK=testnet",
                "export HYPERGROK_MAX_SLIPPAGE_BPS=30",
                'QUOTED="value with spaces"',
                "SINGLE='single'",
                "  SPACED  =  padded  ",
                "no_equals_sign",
            ]
        )
    )
    assert parsed["HYPERGROK_NETWORK"] == "testnet"
    assert parsed["HYPERGROK_MAX_SLIPPAGE_BPS"] == "30"
    assert parsed["QUOTED"] == "value with spaces"
    assert parsed["SINGLE"] == "single"
    assert parsed["SPACED"] == "padded"
    assert "no_equals_sign" not in parsed


def test_load_applies_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("HYPERGROK_TEST_ONLY=from_file\n")
    monkeypatch.delenv("HYPERGROK_TEST_ONLY", raising=False)
    assert load_dotenv(env) == ["HYPERGROK_TEST_ONLY"]
    assert os.environ["HYPERGROK_TEST_ONLY"] == "from_file"


def test_real_environment_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("HYPERGROK_TEST_ONLY=from_file\n")
    monkeypatch.setenv("HYPERGROK_TEST_ONLY", "from_shell")
    assert load_dotenv(env) == []
    assert os.environ["HYPERGROK_TEST_ONLY"] == "from_shell"


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "absent.env") == []


def test_find_walks_upwards(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_dotenv(nested) == tmp_path / ".env"
