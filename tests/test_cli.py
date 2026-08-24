from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_harness.canonical import (
    SEMANTIC_INTENT_HASH_DOMAIN,
    semantic_intent_hash,
)
from trading_harness.cli import build_parser, main
from trading_harness.domain import SemanticIntent


def valid_intent() -> dict[str, object]:
    return {
        "intent_id": "intent-001",
        "thesis_id": "thesis-sma-001",
        "thesis_version": "1",
        "strategy_version": "1",
        "code_hash": "a" * 64,
        "venue": "hyperliquid",
        "account_id": "shadow-account",
        "environment": "shadow",
        "instrument": "ETH-PERP",
        "action": "place_order",
        "side": "buy",
        "quantity": "0.10",
        "order_type": "limit",
        "limit_price": "3000.00",
        "expires_at": "2026-08-21T22:00:00Z",
        "client_order_id": "hg-intent-001",
    }


def run_cli(
    arguments: list[str],
    *,
    stdin: str = "",
) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with (
        patch("sys.stdin", StringIO(stdin)),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_no_live_trading(self) -> None:
        status, stdout, stderr = run_cli(["doctor"])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertTrue(report["ok"])
        self.assertFalse(report["live_trading"])
        self.assertEqual(report["execution"]["adapter"], "disabled")
        self.assertFalse(report["execution"]["venue_writes_enabled"])
        self.assertFalse(report["execution"]["credential_loading_enabled"])

    def test_command_surface_has_no_execution_command(self) -> None:
        parser = build_parser()
        subparser_action = next(
            action for action in parser._actions if action.dest == "command"
        )

        self.assertEqual(
            set(subparser_action.choices),
            {"doctor", "hash-intent", "node"},
        )
        self.assertNotIn("trade", subparser_action.choices)
        self.assertNotIn("execute", subparser_action.choices)

    def test_missing_node_status_is_read_only_and_halted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            status, stdout, stderr = run_cli(
                ["node", "status", "--state-db", str(path)]
            )

            self.assertFalse(path.exists())
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertFalse(report["available"])
        self.assertEqual(report["risk_gate"], "halted")
        self.assertFalse(report["venue_writes_enabled"])


class HashIntentTests(unittest.TestCase):
    def test_hashes_schema_valid_intent_from_stdin(self) -> None:
        document = valid_intent()
        expected = semantic_intent_hash(SemanticIntent.from_mapping(document))

        status, stdout, stderr = run_cli(
            ["hash-intent"], stdin=json.dumps(document)
        )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        self.assertEqual(result["algorithm"], "sha256")
        self.assertEqual(result["domain"], SEMANTIC_INTENT_HASH_DOMAIN)
        self.assertEqual(result["intent_hash"], expected)
        self.assertNotIn("shadow-account", stdout)

    def test_hash_is_stable_across_json_key_order_and_formatting(self) -> None:
        document = valid_intent()
        compact = json.dumps(document, separators=(",", ":"))
        reversed_document = dict(reversed(tuple(document.items())))
        formatted = json.dumps(reversed_document, indent=4)

        first = run_cli(["hash-intent"], stdin=compact)
        second = run_cli(["hash-intent"], stdin=formatted)

        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertEqual(
            json.loads(first[1])["intent_hash"],
            json.loads(second[1])["intent_hash"],
        )

    def test_reads_intent_from_file_without_writing_it(self) -> None:
        original = json.dumps(valid_intent())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "intent.json")
            path.write_text(original, encoding="utf-8")

            status, stdout, stderr = run_cli(["hash-intent", str(path)])

            self.assertEqual(path.read_text(encoding="utf-8"), original)

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(json.loads(stdout)["intent_hash"]), 64)

    def test_rejects_duplicate_json_fields(self) -> None:
        duplicate = '{"intent_id":"first","intent_id":"second"}'

        status, stdout, stderr = run_cli(["hash-intent"], stdin=duplicate)

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("duplicate JSON key: intent_id", stderr)

    def test_rejects_float_monetary_values(self) -> None:
        document = valid_intent()
        document["quantity"] = 0.1

        status, stdout, stderr = run_cli(
            ["hash-intent"], stdin=json.dumps(document)
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("quantity must not be float", stderr)

    def test_rejects_oversized_document(self) -> None:
        status, stdout, stderr = run_cli(
            ["hash-intent"], stdin=" " * 1_000_001
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("exceeds 1,000,000 characters", stderr)


if __name__ == "__main__":
    unittest.main()
