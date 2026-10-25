#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("stop_cost", os.path.join(ROOT, "scripts", "stop_cost.py"))
stop_cost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stop_cost)

CONTEXT = {"markPx": "3001", "oraclePx": "3000", "prevDayPx": "2950", "funding": "0", "openInterest": "100", "dayNtlVlm": "5000000"}
META = [{"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
        [{**CONTEXT, "markPx": "60000"}, CONTEXT]]
# Mid 3000.5. Bids are deep enough for half an ETH at the top; asks thin out fast.
BOOK = {
    "coin": "ETH",
    "time": 1704067200000,
    "levels": [
        [{"px": "3000", "sz": "2", "n": 1}, {"px": "2999", "sz": "3", "n": 1}],
        [{"px": "3001", "sz": "0.1", "n": 1}, {"px": "3010", "sz": "0.2", "n": 1}, {"px": "3040", "sz": "5", "n": 1}],
    ],
}
WORKED = ["--entry", "3000", "--stop", "2900", "--equity", "10200", "--risk-pct", "0.5"]


def cents(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def fixture_dir(test, book=BOOK, coarse=None, meta=META):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    files = {"metaAndAssetCtxs.json": meta, "l2Book-ETH.json": book}
    if coarse is not None:
        files["l2Book-ETH-4sf.json"] = coarse
    for name, payload in files.items():
        with open(os.path.join(directory.name, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    return directory.name


def run(*argv):
    with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
        code = stop_cost.main(list(argv))
    return code, output.getvalue(), errors.getvalue()


class StopCostTest(unittest.TestCase):
    def test_worked_example_reproduces_the_readme_ticket(self):
        priced = stop_cost.price_stop(Decimal("3000"), Decimal("2900"), Decimal("10200"), Decimal("0.5"),
                                      Decimal("0.045"), Decimal("10"), 4)
        self.assertEqual(priced["side"], "long")
        self.assertEqual(priced["stop_fill"], Decimal("2897"))
        self.assertEqual(cents(priced["fees_per_unit"]), Decimal("2.65"))
        self.assertEqual(cents(priced["stressed_distance"]), Decimal("105.65"))
        self.assertEqual(priced["stressed_stop"]["size"], Decimal("0.4827"))
        self.assertEqual(cents(priced["stressed_stop"]["loss_if_triggered"]), Decimal("51.00"))
        self.assertEqual(priced["drawn_stop"]["size"], Decimal("0.5100"))
        self.assertEqual(cents(priced["drawn_stop"]["loss_if_triggered"]), Decimal("53.88"))
        self.assertEqual(priced["drawn_stop"]["loss_pct_of_equity"].quantize(Decimal("0.001")), Decimal("0.528"))
        # The README ticket and desk-risk-limits quote these figures; keep them in step.
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertIn("size 0.4827 ETH", readme)
        self.assertIn("100 + 3.00 slippage + 2.65 fees = 105.65 per ETH", readme)
        self.assertIn("risk $51.00 = 0.5% of equity $10,200", readme)
        with open(os.path.join(ROOT, "skills", "desk-risk-limits", "SKILL.md"), encoding="utf-8") as handle:
            limits = handle.read()
        self.assertIn("Stressed distance is 105.65365 USD/ETH, displayed as 105.65", limits)
        self.assertIn("an assumed loss of $53.88, or 0.528% of equity", limits)

    def test_short_stop_fills_above_and_pays_fees_on_both_legs(self):
        priced = stop_cost.price_stop(Decimal("3000"), Decimal("3100"), Decimal("10200"), Decimal("0.5"),
                                      Decimal("0.045"), Decimal("10"), 4)
        self.assertEqual(priced["side"], "short")
        self.assertEqual(priced["stop_fill"], Decimal("3103"))
        self.assertEqual(priced["fees_per_unit"], (Decimal("3000") + Decimal("3103")) * Decimal("0.00045"))
        self.assertEqual(priced["stressed_distance"], Decimal("103") + priced["fees_per_unit"])
        self.assertLess(priced["stressed_stop"]["size"], priced["drawn_stop"]["size"])

    def test_sizes_round_down_and_never_exceed_the_budget(self):
        for entry, stop in (("3000", "2900"), ("3000", "3100"), ("0.009", "0.0085"), ("117234.5", "115000")):
            with self.subTest(entry=entry, stop=stop):
                priced = stop_cost.price_stop(Decimal(entry), Decimal(stop), Decimal("10200"), Decimal("0.5"),
                                              Decimal("0.045"), Decimal("10"), 2)
                self.assertLessEqual(priced["stressed_stop"]["loss_if_triggered"], priced["risk_usd"])
                self.assertLessEqual(priced["stressed_stop"]["over_budget_usd"], 0)
                self.assertEqual(priced["stressed_stop"]["size"], priced["stressed_stop"]["size"].quantize(Decimal("0.01")))

    def test_long_walks_bids_and_short_walks_asks(self):
        long = stop_cost.exit_impact(stop_cost.fixture_reader(fixture_dir(self), "ETH"), "ETH", Decimal("0.5"), True)
        self.assertTrue(long["complete"])
        self.assertEqual(long["mid"], Decimal("3000.5"))
        self.assertEqual(long["impact_bps"], Decimal("0.5") / Decimal("3000.5") * 10000)
        short = stop_cost.exit_impact(stop_cost.fixture_reader(fixture_dir(self), "ETH"), "ETH", Decimal("0.5"), False)
        vwap = (Decimal("0.1") * 3001 + Decimal("0.2") * 3010 + Decimal("0.2") * 3040) / Decimal("0.5")
        self.assertEqual(short["impact_bps"], (vwap - Decimal("3000.5")) / Decimal("3000.5") * 10000)

    def test_thin_book_raises_slippage_above_the_floor(self):
        code, output, _ = run("--json", "--fixture-dir", fixture_dir(self), "--entry", "3000", "--stop", "3100",
                              "--equity", "10200", "--risk-pct", "0.5", "--slip-bps", "1")
        self.assertEqual(code, 0)
        report = json.loads(output)
        impact = Decimal(report["book"]["impact_bps"])
        self.assertGreater(impact, Decimal(report["book"]["spread_bps"]))
        self.assertEqual(Decimal(report["sizing"]["slip_bps"]), impact)
        self.assertEqual(report["sizing"]["slip_source"], "book")
        self.assertEqual(report["book"]["exit_side"], "ask")

    def test_slippage_is_never_below_the_current_spread(self):
        # desk-risk-limits 2.2: slip_stop is at least the market's current spread.
        code, output, _ = run("--json", "--fixture-dir", fixture_dir(self), *WORKED, "--slip-bps", "0")
        self.assertEqual(code, 0)
        report = json.loads(output)
        spread = Decimal(1) / Decimal("3000.5") * 10000
        self.assertEqual(Decimal(report["book"]["spread_bps"]), spread)
        self.assertLess(Decimal(report["book"]["impact_bps"]), spread)
        self.assertEqual(Decimal(report["sizing"]["slip_bps"]), spread)
        self.assertEqual(report["sizing"]["slip_source"], "spread")

    def test_stop_the_market_cannot_price_is_its_own_error(self):
        code, output, errors = run("--json", "--fixture-dir", fixture_dir(self), "--entry", "1", "--stop", "0.0001",
                                   "--equity", "100", "--risk-pct", "1", "--slip-bps", "0")
        self.assertEqual((code, errors), (1, ""))
        self.assertEqual(json.loads(output)["error"]["code"], "unpriceable_stop")

    def test_uncovered_size_reads_a_coarser_page_then_reports_a_floor(self):
        thin = {**BOOK, "levels": [[{"px": "3000", "sz": "0.1", "n": 1}], BOOK["levels"][1]]}
        coarse = {**BOOK, "levels": [[{"px": "3000", "sz": "0.2", "n": 1}, {"px": "2990", "sz": "0.2", "n": 1}],
                                     [{"px": "3010", "sz": "9", "n": 1}]]}
        reading = stop_cost.exit_impact(stop_cost.fixture_reader(fixture_dir(self, thin, coarse), "ETH"),
                                        "ETH", Decimal("5"), True)
        self.assertFalse(reading["complete"])
        self.assertEqual(reading["resolution"], "4 sig figs")
        self.assertEqual(reading["filled"], Decimal("0.4"))
        covered = stop_cost.exit_impact(stop_cost.fixture_reader(fixture_dir(self, thin, coarse), "ETH"),
                                        "ETH", Decimal("0.3"), True)
        self.assertTrue(covered["complete"])
        self.assertEqual(covered["resolution"], "4 sig figs")

    def test_render_states_the_overspend_and_read_only_boundary(self):
        code, output, errors = run("--fixture-dir", fixture_dir(self), *WORKED)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("HYPERGROK STOP COST · ETH-PERP · LONG", output)
        self.assertIn("2,897.00 = 2,900.00 - 3.00 slippage (10.0 bps of entry, the desk floor)", output)
        self.assertIn("drawn 100.00 · stressed 105.65 per ETH", output)
        self.assertIn("$53.88", output)
        self.assertIn("$2.88 over budget (+5.6%)", output)
        self.assertIn("0.4827 ETH", output)
        self.assertIn("No order created or sent.", output)
        self.assertIn("not a trading signal", output)
        self.assertNotIn("\u2014", output)  # no em dash in printed copy

    def test_render_short_with_whole_lots(self):
        meta = [{"universe": [{"name": "ETH", "szDecimals": 0}]}, [CONTEXT]]
        deep = {**BOOK, "levels": [BOOK["levels"][0], [{"px": "3001", "sz": "50", "n": 1}]]}
        code, output, _ = run("--fixture-dir", fixture_dir(self, deep, meta=meta), "--entry", "3000", "--stop", "3100",
                              "--equity", "31500", "--risk-pct", "1")
        self.assertEqual(code, 0)
        self.assertIn("ETH-PERP · SHORT", output)
        self.assertIn("3 ETH bought from the asks", output)
        self.assertIn("3,103.00 = 3,100.00 + 3.00 slippage", output)
        self.assertRegex(output, r"Drawn stop +3 ETH")
        self.assertRegex(output, r"Stressed stop +2 ETH")

    def test_render_marks_a_shallow_read_as_unproven(self):
        thin = {**BOOK, "levels": [[{"px": "3000", "sz": "0.1", "n": 1}], BOOK["levels"][1]]}
        code, output, _ = run("--fixture-dir", fixture_dir(self, thin), *WORKED)
        self.assertEqual(code, 0)
        self.assertIn("at least", output)
        self.assertIn("the book read does not hold this size", output)
        self.assertIn("budget unproven", output)
        self.assertNotIn("within budget", output)
        coarse = {**BOOK, "levels": [[{"px": "3000", "sz": "9", "n": 1}], BOOK["levels"][1]]}
        code, output, _ = run("--fixture-dir", fixture_dir(self, thin, coarse), *WORKED)
        self.assertIn("(read at 4 sig figs)", output)
        self.assertIn("within budget", output)

    def test_budget_under_one_lot_or_minimum_order_is_flagged(self):
        code, output, _ = run("--fixture-dir", fixture_dir(self), "--entry", "3000", "--stop", "2900",
                              "--equity", "50", "--risk-pct", "0.1")
        self.assertEqual(code, 0)
        self.assertIn("under Hyperliquid's $10.00 minimum order value", output)
        code, output, _ = run("--json", "--fixture-dir", fixture_dir(self), "--entry", "3000", "--stop", "2900",
                              "--equity", "1", "--risk-pct", "0.1")
        self.assertFalse(json.loads(output)["stressed_stop"]["at_least_one_lot"])

    def test_schema_does_not_read_files_or_network(self):
        with patch.object(stop_cost, "fetch_report", side_effect=AssertionError("unexpected read")):
            code, output, _ = run("--schema")
        self.assertEqual(code, 0)
        schema = json.loads(output)
        self.assertEqual(schema["command"], "stop_cost")
        self.assertEqual(schema["mode"], "read-only")

    def test_json_argument_failures_are_structured_and_prevent_reads(self):
        cases = [["--entry", "3000"], [*WORKED, "--stop", "3000"], [*WORKED, "--entry", "nan"],
                 [*WORKED, "--equity", "-1"], [*WORKED, "--risk-pct", "0"], [*WORKED, "--risk-pct", "101"],
                 [*WORKED, "--slip-bps", "10000"], [*WORKED, "--taker-fee", "100"],
                 [*WORKED, "--coin", "xyz:TSLA"], [*WORKED, "--coin", "bad coin"],
                 ["--entry", "1", "--stop", "0.5", "--equity", "100", "--risk-pct", "1", "--slip-bps", "5000"],
                 [*WORKED, "--testnet", "--base-url", "https://example.com"], [*WORKED, "--unknown"]]
        for case in cases:
            with self.subTest(case=case), patch.object(stop_cost, "fetch_report", side_effect=AssertionError("unexpected read")):
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as exit_error:
                    stop_cost.main(["--json", *case])
                self.assertEqual(exit_error.exception.code, 2)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")
                self.assertEqual(errors.getvalue(), "")

    def test_hip3_market_needs_an_explicit_fee(self):
        with patch.object(stop_cost, "fetch_report", return_value={}) as fetch, redirect_stdout(io.StringIO()):
            self.assertEqual(stop_cost.main(["--json", *WORKED, "--coin", "xyz:TSLA", "--taker-fee", "0.09"]), 0)
        self.assertEqual(fetch.call_args.kwargs["plan"]["taker_fee_pct"], Decimal("0.09"))
        self.assertEqual(fetch.call_args.kwargs["dex"], "xyz")

    def test_unavailable_data_is_a_structured_error(self):
        with patch.object(stop_cost, "fetch_report", side_effect=ValueError("ETH order book has no two-sided depth")):
            code, output, errors = run("--json", *WORKED)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"]["code"], "source_unavailable")
        self.assertEqual(errors, "")


if __name__ == "__main__":
    unittest.main()
