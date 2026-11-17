#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from unittest.mock import patch

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


def page(step: str, size: str = "1"):
    """A full 20-level page whose levels walk out from the top of `BOOK` by `step`."""
    bids = [{"px": str(Decimal("2000") - Decimal(i) * Decimal(step)), "sz": size, "n": 1} for i in range(opening_bell.LEVEL_CAP)]
    asks = [{"px": str(Decimal("2001") + Decimal(i) * Decimal(step)), "sz": size, "n": 1} for i in range(opening_bell.LEVEL_CAP)]
    return {"coin": "ETH", "time": 1704067200000, "levels": [bids, asks]}


def capped_book():
    """A full page shaped like ETH's: past the narrow band, short of the wide one."""
    book = page("0.05")
    mid = Decimal("2000.5")
    reach = abs(Decimal(book["levels"][0][-1]["px"]) - mid) / mid * 10000
    assert 5 < reach < 25
    return book


def coarse_book():
    """The same book re-read coarsely: fewer, wider levels that reach past 25 bps."""
    book = page("0.5", size="20")
    mid = Decimal("2000.5")
    assert abs(Decimal(book["levels"][0][-1]["px"]) - mid) / mid * 10000 > 25
    return book


class OpeningBellTest(unittest.TestCase):
    def test_schema_does_not_read_files_or_network(self):
        output = io.StringIO()
        with patch.object(opening_bell, "fetch_snapshot", side_effect=AssertionError("unexpected read")), redirect_stdout(output):
            self.assertEqual(opening_bell.main(["--schema"]), 0)
        schema = json.loads(output.getvalue())
        self.assertEqual(schema["mode"], "read-only")
        self.assertEqual(schema["exit_codes"]["2"], "invalid arguments")

    def test_json_argument_failures_are_structured_and_prevent_reads(self):
        cases = [["--coin", "xyz:TSLA", "--dex", "abc"], ["--timeout", "nan"],
                 ["--timeout", "0"], ["--coin", "bad coin"], ["--timeout", "nope"],
                 ["--testnet", "--base-url", "https://example.com"],
                 ["--base-url", "https://testnet.example.com/path"], ["--unknown"]]
        for case in cases:
            with self.subTest(case=case), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                with patch.object(opening_bell, "fetch_snapshot", side_effect=AssertionError("unexpected read")), self.assertRaises(SystemExit) as exit_error:
                    opening_bell.main(["--json", *case])
                self.assertEqual(exit_error.exception.code, 2)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_arguments")
                self.assertEqual(errors.getvalue(), "")

    def test_json_unavailable_data_is_not_a_successful_empty_snapshot(self):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            with patch.object(opening_bell, "fetch_snapshot", side_effect=ValueError("missing market")):
                self.assertEqual(opening_bell.main(["--json"]), 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "source_unavailable")
        self.assertEqual(errors.getvalue(), "")

    def test_hip3_queries_use_canonical_prefixed_coin_and_dex(self):
        meta = [{"universe": [{"name": "xyz:TSLA", "szDecimals": 3, "maxLeverage": 10}]}, [META[1][1]]]
        book = {**BOOK, "coin": "xyz:TSLA"}
        with patch.object(opening_bell, "post_info", return_value=meta) as request, patch.object(opening_bell, "fetch_books", return_value=[(None, book)]) as books:
            snapshot = opening_bell.fetch_snapshot("tsla", opening_bell.DEFAULT_BASE_URL, 15, dex="xyz")
        request.assert_called_once_with(opening_bell.DEFAULT_BASE_URL, {"type": "metaAndAssetCtxs", "dex": "xyz"}, 15, opening_bell.USER_AGENT)
        books.assert_called_once_with("xyz:TSLA", opening_bell.DEFAULT_BASE_URL, 15)
        self.assertEqual(snapshot["coin"], "xyz:TSLA")
        self.assertEqual(snapshot["dex"], "xyz")
        self.assertEqual(snapshot["metadata"]["szDecimals"], 3)

    def test_prefixed_coin_selects_dex_without_case_damage(self):
        with patch.object(opening_bell, "fetch_snapshot", return_value={}) as fetch, redirect_stdout(io.StringIO()):
            self.assertEqual(opening_bell.main(["--json", "--coin", "xyz:TSLA"]), 0)
        self.assertEqual(fetch.call_args.args, ("xyz:TSLA", opening_bell.DEFAULT_BASE_URL, 15.0, None, "xyz"))

    def test_coin_resolution_preserves_mixed_case_and_refuses_unknown_delisted_ambiguous(self):
        meta = [{"universe": [{"name": "kPEPE"}]}, [META[1][1]]]
        self.assertEqual(opening_bell.resolve_coin(meta, "KPEPE"), "kPEPE")
        with self.assertRaisesRegex(ValueError, "OTHER is not listed in the main perp universe"):
            opening_bell.resolve_coin(meta, "OTHER")
        with self.assertRaisesRegex(ValueError, "xyz:OTHER is not listed on the xyz DEX"):
            opening_bell.resolve_coin(meta, "OTHER", "xyz")
        meta[0]["universe"][0]["isDelisted"] = True
        with self.assertRaisesRegex(ValueError, "delisted"):
            opening_bell.resolve_coin(meta, "kpepe")
        meta[0]["universe"].append({"name": "KPEPE"})
        meta[1].append(META[1][1])
        with self.assertRaisesRegex(ValueError, "more than one market"):
            opening_bell.resolve_coin(meta, "kpepe")
        with self.assertRaisesRegex(ValueError, "delisted"):
            opening_bell.resolve_coin(meta, "kPEPE")  # the exact spelling selects one market

    def test_custom_origin_is_not_labelled_mainnet_or_testnet(self):
        with patch.object(opening_bell, "post_info", return_value=META), patch.object(opening_bell, "fetch_books", return_value=[(None, BOOK)]):
            snapshot = opening_bell.fetch_snapshot("ETH", "https://testnet.example.com", 15)
        self.assertEqual(snapshot["network"], "custom")
        self.assertEqual(snapshot["source"]["base_url"], "https://testnet.example.com")

    def test_malformed_source_contexts_return_structured_json(self):
        cases = []
        for previous in ("0", "-1", "NaN"):
            meta = json.loads(json.dumps(META))
            meta[1][1]["prevDayPx"] = previous
            cases.append(meta)
        cases.extend(([None, []], [{"universe": None}, []],
                      [{"universe": [{"name": "ETH"}]}, [None]],
                      [{"universe": [{"name": "ETH"}]}, []]))
        for meta in cases:
            with self.subTest(meta=meta), patch.object(opening_bell, "post_info", return_value=meta), patch.object(opening_bell, "fetch_books", return_value=[(None, BOOK)]), redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(opening_bell.main(["--json"]), 1)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "source_unavailable")
                self.assertEqual(errors.getvalue(), "")

    def test_snapshot_is_read_only_and_computes_depth(self):
        snapshot = opening_bell.build_snapshot(META, [(None, BOOK)], "ETH", "fixture")
        self.assertEqual(snapshot["mode"], "read-only")
        self.assertEqual(snapshot["prices"]["book_mid"], "2000.5")
        self.assertEqual(Decimal(snapshot["book"]["depth"]["5"]["bid_base"]), Decimal("2"))
        self.assertEqual(Decimal(snapshot["book"]["depth"]["25"]["ask_base"]), Decimal("9"))
        self.assertIn("No order", snapshot["safety"])

    def test_render_labels_sources_and_no_signal(self):
        text = opening_bell.render(opening_bell.build_snapshot(META, [(None, BOOK)], "ETH", "fixture"))
        self.assertIn("HYPERGROK OPENING BELL", text)
        self.assertIn("metaAndAssetCtxs + l2Book", text)
        self.assertIn("not a trading signal", text)

    def test_render_keeps_the_figures_of_a_price_below_one(self):
        meta = [{"universe": [{"name": "kPEPE"}]},
                [{"markPx": "0.003863", "oraclePx": "0.003861", "prevDayPx": "0.0038", "funding": "0", "openInterest": "1", "dayNtlVlm": "1"}]]
        book = {"coin": "kPEPE", "time": 1704067200000, "levels": [[{"px": "0.003862", "sz": "1", "n": 1}], [{"px": "0.003864", "sz": "1", "n": 1}]]}
        text = opening_bell.render(opening_bell.build_snapshot(meta, [(None, book)], "kPEPE", "fixture"))
        self.assertIn("mid 0.003863 · mark 0.003863 · oracle 0.003861", text)
        self.assertNotIn("\u2014", text)  # no em dash in printed copy

    def test_failed_reads_are_reported_in_plain_words(self):
        import urllib.error
        cases = ((urllib.error.HTTPError("u", 500, "Internal Server Error", None, None), "HTTP 500, which it does for a coin or DEX it does not list"),
                 (urllib.error.URLError(TimeoutError("timed out")), "timed out; check the connection or raise --timeout"),
                 (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), "cannot reach the API: Connection refused"))
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(opening_bell, "post_info", side_effect=error), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(opening_bell.main(["--coin", "nodex:ETH"]), 1)
            self.assertIn(expected, errors.getvalue())
            self.assertNotIn("urlopen", errors.getvalue())

    def test_fixture_cli_path(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (("metaAndAssetCtxs.json", META), ("l2Book-ETH.json", BOOK)):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(opening_bell.main(["--fixture-dir", directory, "--coin", "ETH", "--json"]), 0)

    def test_short_book_reports_every_band_as_measured(self):
        book = opening_bell.build_snapshot(META, [(None, BOOK)], "ETH", "fixture")["book"]
        self.assertLess(book["pages"][0]["levels_returned"]["bid"], opening_bell.LEVEL_CAP)
        for band in ("5", "10", "25"):
            self.assertTrue(book["depth"][band]["bid_complete"], band)
            self.assertTrue(book["depth"][band]["ask_complete"], band)
        self.assertNotIn(">=", opening_bell.render(opening_bell.build_snapshot(META, [(None, BOOK)], "ETH", "fixture")))

    def test_capped_book_marks_bands_beyond_its_reach_as_floors(self):
        snapshot = opening_bell.build_snapshot(META, [(None, capped_book())], "ETH", "fixture")
        book = snapshot["book"]
        self.assertEqual(book["pages"][0]["levels_returned"], {"bid": opening_bell.LEVEL_CAP, "ask": opening_bell.LEVEL_CAP})
        self.assertLess(Decimal(book["pages"][0]["reach_bps"]["bid"]), 25)
        self.assertFalse(book["depth"]["25"]["bid_complete"])
        self.assertFalse(book["depth"]["25"]["ask_complete"])
        # The cut-off bands repeat the same total; without the marker that reads as a flat book.
        self.assertEqual(book["depth"]["25"]["bid_base"], book["depth"]["10"]["bid_base"])
        text = opening_bell.render(snapshot)
        self.assertIn(">= $", text)
        self.assertIn("floors, not totals", text)

    def test_coarser_page_measures_the_band_the_full_page_cannot(self):
        pages = [(None, capped_book()), (4, coarse_book())]
        book = opening_bell.build_snapshot(META, pages, "ETH", "fixture")["book"]
        # Top of book still comes from the full-precision page.
        self.assertEqual(Decimal(book["best_bid"]), Decimal("2000"))
        self.assertEqual(Decimal(book["best_ask"]), Decimal("2001"))
        # The narrow band the full page reaches is still measured at full precision.
        self.assertTrue(book["depth"]["5"]["bid_complete"])
        self.assertEqual(book["depth"]["5"]["bid_source"], opening_bell.FULL_PRECISION)
        # The widest band is now a total read off the coarser page, not a floor.
        self.assertTrue(book["depth"]["25"]["bid_complete"])
        self.assertTrue(book["depth"]["25"]["ask_complete"])
        self.assertEqual(book["depth"]["25"]["bid_source"], "4 sig figs")
        self.assertGreater(Decimal(book["depth"]["25"]["bid_base"]), Decimal(book["depth"]["5"]["bid_base"]))
        text = opening_bell.render(opening_bell.build_snapshot(META, pages, "ETH", "fixture"))
        self.assertNotIn(">= $", text)
        self.assertIn("re-read at 4 sig figs", text)
        self.assertIn("(4 sig figs)", text)

    def test_ladder_stops_once_the_widest_band_is_covered(self):
        mid = Decimal("2000.5")
        reaching = opening_bell.page_view(None, coarse_book(), "ETH", mid)
        capped = opening_bell.page_view(None, capped_book(), "ETH", mid)
        self.assertTrue(opening_bell.measures([reaching], max(opening_bell.BANDS_BPS)))
        self.assertFalse(opening_bell.measures([capped], max(opening_bell.BANDS_BPS)))
        self.assertTrue(opening_bell.measures([capped, reaching], max(opening_bell.BANDS_BPS)))

    def test_fixture_dir_loads_coarser_pages_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (
                ("metaAndAssetCtxs.json", META),
                ("l2Book-ETH.json", capped_book()),
                ("l2Book-ETH-4sf.json", coarse_book()),
            ):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            pages = opening_bell.load_books(directory, "ETH")
            self.assertEqual([resolution for resolution, _ in pages], [None, 4])

    def test_null_book_is_reported_not_raised_as_a_traceback(self):
        # `l2Book` answers `null` for a coin or resolution it cannot page.
        with self.assertRaisesRegex(ValueError, "two-sided depth"):
            opening_bell.build_snapshot(META, [(None, None)], "ETH", "fixture")

    def test_unusable_coarser_page_falls_back_to_floors(self):
        pages = [(None, capped_book()), (4, None)]
        book = opening_bell.build_snapshot(META, pages, "ETH", "fixture")["book"]
        self.assertEqual([p["resolution"] for p in book["pages"]], [opening_bell.FULL_PRECISION])
        self.assertTrue(book["depth"]["5"]["bid_complete"])
        self.assertFalse(book["depth"]["25"]["bid_complete"])
        self.assertIn(">= $", opening_bell.render(opening_bell.build_snapshot(META, pages, "ETH", "fixture")))

    def test_unknown_coin_is_named_before_the_book_is_paged(self):
        with self.assertRaisesRegex(ValueError, "not in the default perp universe"):
            opening_bell.perp_context(META, "NOTACOIN")

    def test_rejects_crossed_book(self):
        crossed = json.loads(json.dumps(BOOK))
        crossed["levels"][0][0]["px"] = "2002"
        with self.assertRaisesRegex(ValueError, "invalid"):
            opening_bell.build_snapshot(META, [(None, crossed)], "ETH", "fixture")


if __name__ == "__main__":
    unittest.main()
