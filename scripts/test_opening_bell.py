#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("opening_bell", os.path.join(ROOT, "scripts", "opening_bell.py"))
opening_bell = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(opening_bell)


META = [
    {"universe": [{"name": "BTC"}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
    [
        {"markPx": "60000", "oraclePx": "60000", "prevDayPx": "59000", "funding": "0", "openInterest": "1", "dayNtlVlm": "1"},
        {"midPx": "2000.5", "markPx": "2001", "oraclePx": "2000", "prevDayPx": "1900", "funding": "0.00001", "openInterest": "100", "dayNtlVlm": "5000000"},
    ],
]
BOOK = {
    "coin": "ETH",
    "time": 1704067200000,
    "levels": [
        [{"px": "2000", "sz": "2", "n": 1}, {"px": "1998", "sz": "3", "n": 1}, {"px": "1990", "sz": "9", "n": 1}],
        [{"px": "2001", "sz": "4", "n": 1}, {"px": "2003", "sz": "5", "n": 1}, {"px": "2011", "sz": "9", "n": 1}],
    ],
}


class OpeningBellTest(unittest.TestCase):
    def test_snapshot_is_read_only_and_computes_depth(self):
        snapshot = opening_bell.build_snapshot(META, BOOK, "ETH", "fixture")
        self.assertEqual(snapshot["mode"], "read-only")
        self.assertEqual(snapshot["prices"]["book_mid"], "2000.5")
        self.assertEqual(Decimal(snapshot["book"]["depth"]["5"]["bid_base"]), Decimal("2"))
        self.assertEqual(Decimal(snapshot["book"]["depth"]["25"]["ask_base"]), Decimal("9"))
        self.assertIn("No order", snapshot["safety"])

    def test_render_labels_sources_and_no_signal(self):
        text = opening_bell.render(opening_bell.build_snapshot(META, BOOK, "ETH", "fixture"))
        self.assertIn("HYPERGROK OPENING BELL", text)
        self.assertIn("metaAndAssetCtxs + l2Book", text)
        self.assertIn("not a trading signal", text)

    def test_fixture_cli_path(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (("metaAndAssetCtxs.json", META), ("l2Book-ETH.json", BOOK)):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(opening_bell.main(["--fixture-dir", directory, "--coin", "ETH", "--json"]), 0)

    def test_rejects_crossed_book(self):
        crossed = json.loads(json.dumps(BOOK))
        crossed["levels"][0][0]["px"] = "2002"
        with self.assertRaisesRegex(ValueError, "invalid"):
            opening_bell.build_snapshot(META, crossed, "ETH", "fixture")


if __name__ == "__main__":
    unittest.main()
