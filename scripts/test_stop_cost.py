#!/usr/bin/env python3
import importlib.util
import io
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from fractions import Fraction
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

    def test_render_groups_thousands_in_large_sizes(self):
        deep = {**BOOK, "levels": [[{"px": "3000", "sz": "50000", "n": 1}], BOOK["levels"][1]]}
        code, output, _ = run("--fixture-dir", fixture_dir(self, deep), "--entry", "3000", "--stop", "2900",
                              "--equity", "50000000", "--risk-pct", "5")
        self.assertEqual(code, 0)
        self.assertIn("25,000.0000 ETH sold into the bids", output)
        self.assertRegex(output, r"Drawn stop +25,000\.0000 ETH")

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

    def test_boolean_sz_decimals_is_not_a_lot_size(self):
        for flag in (True, False):
            with self.subTest(sz_decimals=flag):
                meta = [{"universe": [{"name": "ETH", "szDecimals": flag}]}, [CONTEXT]]
                code, output, errors = run("--json", "--fixture-dir", fixture_dir(self, meta=meta), *WORKED)
                body = json.loads(output)
                self.assertEqual(code, 1)
                self.assertEqual(errors, "")
                self.assertEqual(body["error"]["code"], "source_unavailable")
                self.assertIn("szDecimals", body["error"]["message"])

    def test_unavailable_data_is_a_structured_error(self):
        with patch.object(stop_cost, "fetch_report", side_effect=ValueError("ETH order book has no two-sided depth")):
            code, output, errors = run("--json", *WORKED)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"]["code"], "source_unavailable")
        self.assertEqual(errors, "")


# Seeded generated inputs for the properties below. A failure prints its case, and the
# fixed seed regenerates the same cases on every run.
SEED = 20261117
CASES = 1500


def random_decimal(rng, low, high, max_digits=7):
    digits = rng.randint(1, 10 ** rng.randint(1, max_digits) - 1)
    return Decimal(digits).scaleb(rng.randint(low, high) - len(str(digits)) + 1)


def random_plan(rng):
    """A long or short plan with the stop up to 50% from an entry between 1e-4 and 1e6."""
    entry = random_decimal(rng, -4, 6)
    while True:
        stop = (entry * (1 + Decimal(rng.randint(-5000, 5000)) / Decimal(10000))).quantize(
            Decimal(1).scaleb(entry.adjusted() - 5))
        if stop > 0 and stop != entry:
            break
    return {
        "entry": entry, "stop": stop, "equity": random_decimal(rng, 1, 8, 6),
        "risk_pct": Decimal(rng.randint(1, 1000)) / Decimal(100),
        "taker_fee_pct": Decimal(rng.randint(0, 100)) / Decimal(1000),
        "slip_bps": Decimal(rng.randint(0, 500)), "sz_decimals": rng.randint(0, 6),
    }


def random_book(rng, mid, levels=None):
    """A two-sided book around `mid`: bids descend, asks ascend, sizes are positive."""
    step = mid / Decimal(rng.choice((2000, 10000, 50000)))
    tick = Decimal(1).scaleb(mid.adjusted() - 6)
    sides = []
    for direction in (-1, 1):
        price, side = mid, []
        for _ in range(levels or rng.randint(1, 8)):
            price += direction * step * rng.randint(1, 5)
            side.append({"px": str(price.quantize(tick)), "sz": str(random_decimal(rng, -3, 3, 4)), "n": 1})
        sides.append(side)
    return {"coin": "ETH", "time": 1704067200000, "levels": sides}


class GeneratedStopCostTest(unittest.TestCase):
    def test_sizing_matches_exact_fractions_and_never_exceeds_the_budget(self):
        rng = random.Random(SEED)
        priced_cases = 0
        for index in range(CASES):
            plan = random_plan(rng)
            case = f"seed={SEED} case={index} {plan}"
            entry, stop = Fraction(plan["entry"]), Fraction(plan["stop"])
            long = stop < entry
            slip = entry * Fraction(plan["slip_bps"]) / 10000
            stop_fill = stop - slip if long else stop + slip
            try:
                priced = stop_cost.price_stop(plan["entry"], plan["stop"], plan["equity"], plan["risk_pct"],
                                              plan["taker_fee_pct"], plan["slip_bps"], plan["sz_decimals"])
            except stop_cost.UnpriceableStop:
                self.assertLessEqual(stop_fill, 0, case)
                continue
            priced_cases += 1
            self.assertGreater(stop_fill, 0, case)
            # The same arithmetic in exact fractions, with the size floored to whole lots.
            stressed = abs(entry - stop_fill) + (entry + stop_fill) * Fraction(plan["taker_fee_pct"]) / 100
            budget = Fraction(plan["equity"]) * Fraction(plan["risk_pct"]) / 100
            lot = Fraction(1, 10 ** plan["sz_decimals"])
            self.assertEqual(priced["side"], "long" if long else "short", case)
            self.assertEqual(Fraction(priced["stop_fill"]), stop_fill, case)
            self.assertEqual(Fraction(priced["stressed_distance"]), stressed, case)
            self.assertEqual(Fraction(priced["risk_usd"]), budget, case)
            for key, distance in (("stressed_stop", stressed), ("drawn_stop", abs(entry - stop))):
                row = priced[key]
                self.assertEqual(Fraction(row["size"]), math.floor(budget / distance / lot) * lot, f"{case} {key}")
                # Decimal keeps 28 digits, so a very large position's loss is exact to that precision.
                exact_loss = Fraction(row["size"]) * stressed
                self.assertLessEqual(abs(Fraction(row["loss_if_triggered"]) - exact_loss),
                                     exact_loss / 10 ** 27, f"{case} {key}")
                self.assertEqual(row["at_least_one_lot"], Fraction(row["size"]) >= lot, f"{case} {key}")
                self.assertEqual(row["meets_min_order_value"], Fraction(row["size"]) * entry >= 10, f"{case} {key}")
            sized, drawn = priced["stressed_stop"], priced["drawn_stop"]
            self.assertLessEqual(sized["over_budget_usd"], 0, case)
            self.assertLessEqual(sized["loss_if_triggered"], priced["risk_usd"], case)
            # One more lot would break the budget, so the size is not needlessly small.
            self.assertGreater((Fraction(sized["size"]) + lot) * stressed, budget, case)
            self.assertLessEqual(sized["size"], drawn["size"], case)
            self.assertGreaterEqual(drawn["loss_if_triggered"], sized["loss_if_triggered"], case)
        self.assertGreater(priced_cases, CASES // 2)

    def test_walk_fills_what_the_side_holds_at_the_exact_notional(self):
        rng = random.Random(SEED + 1)
        for index in range(CASES):
            book = random_book(rng, random_decimal(rng, -3, 5))
            side = book["levels"][rng.randint(0, 1)]
            size = random_decimal(rng, -3, 4, 4)
            case = f"seed={SEED + 1} case={index} size={size} side={side}"
            filled, notional = stop_cost.walk(side, size)
            remaining, expected = Fraction(size), Fraction(0)
            for level in side:
                take = min(remaining, Fraction(level["sz"]))
                expected += take * Fraction(level["px"])
                remaining -= take
            self.assertEqual(Fraction(filled), Fraction(size) - remaining, case)
            self.assertEqual(Fraction(notional), expected, case)
            prices = [Decimal(level["px"]) for level in side]
            self.assertTrue(min(prices) * filled <= notional <= max(prices) * filled, case)
            # A larger order never gets a better average on the side it exits into.
            more_filled, more_notional = stop_cost.walk(side, size * 2)
            worse = Fraction(more_notional) / Fraction(more_filled) - expected / Fraction(filled)
            self.assertTrue(worse <= 0 if side is book["levels"][0] else worse >= 0, case)

    def test_report_uses_the_largest_slippage_source_and_stays_within_budget(self):
        rng = random.Random(SEED + 2)
        reports = 0
        for index in range(300):
            plan = random_plan(rng)
            sz_decimals = plan["sz_decimals"]
            book = random_book(rng, plan["entry"])
            meta = [{"universe": [{"name": "ETH", "szDecimals": sz_decimals}]},
                    [{"markPx": str(plan["entry"])}]]
            case = f"seed={SEED + 2} case={index} {plan} book={book['levels']}"
            cli_plan = {**{key: plan[key] for key in ("entry", "stop", "equity", "risk_pct", "taker_fee_pct")},
                        "slip_floor_bps": plan["slip_bps"]}
            try:
                report = stop_cost.build_report(meta, "ETH", lambda n_sig_figs: None if n_sig_figs else book,
                                                "fixture", "", cli_plan)
            except stop_cost.UnpriceableStop:
                continue
            reports += 1
            long = plan["stop"] < plan["entry"]
            self.assertEqual(report["side"], "long" if long else "short", case)
            self.assertEqual(report["book"]["exit_side"], "bid" if long else "ask", case)
            side = book["levels"][0 if long else 1]
            walked = Decimal(report["book"]["size_walked"])
            self.assertEqual(report["book"]["impact_complete"],
                             sum(Decimal(level["sz"]) for level in side) >= walked, case)
            spread, impact = Decimal(report["book"]["spread_bps"]), Decimal(report["book"]["impact_bps"])
            # The exit side starts half a spread from the mid, so a fill is never closer than that.
            self.assertGreaterEqual(impact * 2, spread * (1 - Decimal("1e-20")), case)
            applied = Decimal(report["sizing"]["slip_bps"])
            self.assertEqual(applied, max(plan["slip_bps"], spread, impact), case)
            source = {"floor": plan["slip_bps"], "spread": spread, "book": impact}[report["sizing"]["slip_source"]]
            self.assertEqual(source, applied, case)
            self.assertLessEqual(Decimal(report["stressed_stop"]["over_budget_usd"]), 0, case)
            self.assertLessEqual(Decimal(report["stressed_stop"]["size"]), Decimal(report["drawn_stop"]["size"]), case)
            self.assertEqual(json.loads(json.dumps(report)), report, case)
            rendered = stop_cost.render(report)
            self.assertIn("Stressed stop", rendered, case)
            self.assertNotRegex(rendered, r"\d[Ee][+-]?\d", case)
        self.assertGreater(reports, 150)

    def test_printed_prices_never_use_an_exponent_and_keep_four_figures(self):
        rng = random.Random(SEED + 3)
        for index in range(CASES):
            value = random_decimal(rng, -12, 9, 12)
            case = f"seed={SEED + 3} case={index} value={value}"
            shown = stop_cost.price(value)
            self.assertRegex(shown, r"^[0-9][0-9,]*\.[0-9]+$", case)
            printed = Decimal(shown.replace(",", ""))
            # Two decimals from 1 up; four significant figures below.
            tolerance = Decimal("0.005") if value >= 1 else Decimal(1).scaleb(value.adjusted() - 3) / 2
            self.assertLessEqual(abs(printed - value), tolerance, case)


if __name__ == "__main__":
    unittest.main()
