#!/usr/bin/env python3
"""Offline behavioral checks for reproducible public research datasets."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import BadStatusLine, IncompleteRead
from unittest.mock import Mock, patch

import research_dataset as dataset


START = 1_704_067_200_000  # 2024-01-01T00:00:00Z
HOUR = dataset.HOUR
META = {"universe": [{"name": "ETH"}]}


def parameters(hours=3, **overrides):
    values = {"coin": "ETH", "dex": None, "interval": "1h", "start_ms": START,
              "end_ms": START + hours * HOUR, "as_of_ms": START + (hours + 1) * HOUR,
              "with_funding": False, "max_pages": 100,
              "base_url": "https://api.hyperliquid.xyz", "network": "mainnet"}
    values.update(overrides)
    return values


def bar(index=0, **overrides):
    values = {"t": START + index * HOUR, "T": START + (index + 1) * HOUR - 1,
              "s": "ETH", "i": "1h", "o": "2000.000000000000000001",
              "h": "2001.000000000000000003", "l": "1999.000000000000000001",
              "c": "2000.000000000000000002", "v": "1.000000000000000009", "n": 2}
    values.update(overrides)
    return values


def rate(index=0, **overrides):
    # Offset varies deliberately: funding timestamps are not exact UTC hours.
    values = {"coin": "ETH", "time": START + index * HOUR + 18 + index % 45,
              "fundingRate": "-0.00001234567890123456789", "premium": "0.000000000000000000001"}
    values.update(overrides)
    return values


class FixtureReader:
    """Capture injected responses just as a real reader would, without I/O."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.records = []

    def __call__(self, payload):
        if not self.responses:
            raise AssertionError("unexpected extra public read")
        response = self.responses.pop(0)
        record = {"request": copy.deepcopy(payload), "retrieved_at_utc": "2024-01-02T00:00:00.000Z"}
        self.records.append(record)
        if isinstance(response, Exception):
            record["error"] = str(response)
            raise dataset.SourceError(str(response))
        record["response"] = copy.deepcopy(response)
        return copy.deepcopy(response)


def complete_reader(hours=3, funding=False):
    responses = [META, [bar(i) for i in range(hours)]]
    if funding:
        responses.append([rate(i) for i in range(hours)])
    return FixtureReader(*responses)


def write_bundle(directory, p=None, reader=None):
    p = p or parameters()
    reader = reader or complete_reader()
    result, candles, funding = dataset.collect(p, reader)
    manifest, files = dataset.bundle_content(p, reader.records, result, candles, funding)
    for name, content in files.items():
        (Path(directory) / name).write_bytes(content)
    return manifest


class CollectionTest(unittest.TestCase):
    def test_complete_dataset_preserves_exact_decimal_strings_and_request_bounds(self):
        reader = complete_reader(funding=True)
        result, candles, funding = dataset.collect(parameters(with_funding=True), reader)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["candles"]["observed"], 3)
        self.assertEqual(result["coverage"]["funding"]["observed"], 3)
        self.assertEqual(candles, [bar(i) for i in range(3)])
        self.assertEqual(funding, [rate(i) for i in range(3)])
        self.assertEqual(reader.records[1]["request"], {"type": "candleSnapshot", "req": {
            "coin": "ETH", "interval": "1h", "startTime": START, "endTime": START + 3 * HOUR - 1}})
        self.assertEqual(reader.records[2]["request"], {"type": "fundingHistory", "coin": "ETH",
            "startTime": START, "endTime": START + 3 * HOUR - 1})
        csv_rows = list(csv.DictReader(io.StringIO(dataset.csv_bytes(candles, dataset.CANDLE_FIELDS).decode())))
        self.assertEqual(csv_rows[0]["c"], bar()["c"])
        self.assertEqual(csv_rows[0]["v"], bar()["v"])

    def test_outside_and_current_bars_are_excluded_without_changing_window(self):
        reader = FixtureReader(META, [bar(3), bar(1), bar(-1), bar(0), bar(2)])
        p = parameters(as_of_ms=START + 3 * HOUR)
        result, candles, _ = dataset.collect(p, reader)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["candles_excluded_outside_window"], 2)
        self.assertEqual([row["t"] for row in candles], [START + i * HOUR for i in range(3)])

    def test_all_supported_fixed_intervals_close_at_the_exclusive_end(self):
        for interval, step in dataset.INTERVALS.items():
            with self.subTest(interval=interval):
                end = START + 2 * step
                rows = [bar(t=START + i * step, T=START + (i + 1) * step - 1, i=interval) for i in range(2)]
                result, candles, _ = dataset.collect(
                    parameters(interval=interval, end_ms=end, as_of_ms=end), FixtureReader(META, rows))
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["coverage"]["candles"]["expected"], 2)
                self.assertEqual(candles[-1]["T"], end - 1)

    def test_missing_first_middle_last_and_empty_history_remain_incomplete(self):
        cases = [([], [{"start_ms": START, "end_ms": START + 3 * HOUR, "count": 3}])]
        for missing in range(3):
            cases.append(([bar(i) for i in range(3) if i != missing], [{
                "start_ms": START + missing * HOUR, "end_ms": START + (missing + 1) * HOUR, "count": 1}]))
        for rows, missing_ranges in cases:
            with self.subTest(rows=len(rows), missing=missing_ranges):
                result, _, _ = dataset.collect(parameters(), FixtureReader(META, rows))
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["coverage"]["candles"]["missing_ranges"], missing_ranges)

    def test_identical_candle_duplicates_are_removed_and_conflicts_rejected(self):
        result, candles, _ = dataset.collect(parameters(), FixtureReader(META, [bar(0), bar(1), bar(1), bar(2)]))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["duplicates_removed"]["candles"], 1)
        self.assertEqual(len(candles), 3)
        result, _, _ = dataset.collect(parameters(), FixtureReader(META, [bar(), bar(c="2000.5")]))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("conflicting", result["issues"][0])

    def test_invalid_candle_fields_cannot_become_successful_partial_data(self):
        cases = [None, {}, bar(s="BTC"), bar(i="4h"), bar(t=START + 1), bar(T=START + HOUR),
                 bar(o="NaN"), bar(c="Infinity"), bar(l="2001"), bar(h="1000"), bar(o="0"),
                 bar(v="-1"), bar(n=True), bar(n=-1), bar(c=2000.0), bar(t=True), bar(v="x")]
        for row in cases:
            with self.subTest(row=row):
                result, _, _ = dataset.collect(parameters(), FixtureReader(META, [row]))
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(result["issues"])

    def test_bad_response_shapes_and_market_resolution_are_unavailable(self):
        for meta in (None, [], {}, {"universe": None}, {"universe": [None]},
                     {"universe": [{"name": "BTC"}]},
                     {"universe": [{"name": "ETH"}, {"name": "eth"}]},
                     {"universe": [{"name": "ETH", "isDelisted": "false"}]}):
            with self.subTest(meta=meta):
                reader = FixtureReader(meta)
                result, _, _ = dataset.collect(parameters(), reader)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(len(reader.records), 1)
        for payload in (None, {}, "[]", [bar()] * 10_003):
            with self.subTest(payload_type=type(payload).__name__):
                result, _, _ = dataset.collect(parameters(), FixtureReader(META, payload))
                self.assertEqual(result["status"], "unavailable")

    def test_hip3_resolution_preserves_canonical_coin_and_delisted_history(self):
        meta = {"universe": [{"name": "xyz:MixedCase", "isDelisted": True}]}
        reader = FixtureReader(meta, [bar(i, s="xyz:MixedCase") for i in range(3)])
        result, _, _ = dataset.collect(parameters(coin="mixedcase", dex="xyz"), reader)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["market"], {"coin": "xyz:MixedCase", "dex": "xyz", "is_delisted": True})
        self.assertEqual(reader.records[0]["request"], {"type": "meta", "dex": "xyz"})
        self.assertEqual(reader.records[1]["request"]["req"]["coin"], "xyz:MixedCase")

    def test_invalid_parameters_reject_before_any_reads(self):
        cases = [{"start_ms": START + 1}, {"end_ms": START}, {"as_of_ms": START},
                 {"end_ms": START + 5001 * HOUR, "as_of_ms": START + 5002 * HOUR},
                 {"coin": "@107"}, {"coin": "xyz:ETH", "dex": "abc"}, {"dex": "XYZ"},
                 {"interval": "1M"}, {"with_funding": 1}, {"max_pages": 0}, {"max_pages": 251},
                 {"max_pages": True}, {"network": "testnet"}, {"start_ms": True},
                 {"base_url": "https://api.hyperliquid.xyz/exchange"},
                 {"interval": "1m", "end_ms": START + 60_000, "with_funding": True}]
        for changes in cases:
            with self.subTest(changes=changes):
                reader = Mock(side_effect=AssertionError("invalid parameters reached the network"))
                with self.assertRaises(ValueError):
                    dataset.collect(parameters(**changes), reader)
                reader.assert_not_called()

    def test_captured_source_failure_preserves_prior_candles_as_unavailable(self):
        reader = FixtureReader(META, [bar(i) for i in range(3)], dataset.SourceError("offline fixture failure"))
        result, candles, funding = dataset.collect(parameters(with_funding=True), reader)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(candles), 3)
        self.assertEqual(funding, [])
        self.assertIn("offline fixture failure", result["issues"])


class FundingTest(unittest.TestCase):
    def collect_rates(self, rows, hours=3, **overrides):
        return dataset.collect(parameters(hours, with_funding=True, **overrides),
                               FixtureReader(META, [bar(i) for i in range(hours)], rows))

    def test_inclusive_pagination_preserves_offsets_and_removes_overlap(self):
        # More than one actual API page, with one intentional inclusive overlap.
        rates = [rate(i) for i in range(1001)]
        reader = FixtureReader(META, [bar(i) for i in range(1001)], rates[:500], rates[499:999], rates[998:])
        result, _, funding = dataset.collect(parameters(1001, with_funding=True), reader)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(funding, rates)
        self.assertEqual(result["duplicates_removed"]["funding"], 2)
        self.assertEqual([r["request"]["startTime"] for r in reader.records[2:]],
                         [START, rates[499]["time"], rates[998]["time"]])

    def test_exact_full_page_requires_followup_before_success(self):
        rates = [rate(i) for i in range(500)]
        reader = FixtureReader(META, [bar(i) for i in range(500)], rates, [rates[-1]])
        result, _, funding = dataset.collect(parameters(500, with_funding=True), reader)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(reader.records), 4)
        self.assertEqual(len(funding), 500)
        self.assertEqual(result["duplicates_removed"]["funding"], 1)

    def test_empty_and_missing_hour_are_incomplete_even_with_complete_candles(self):
        for rows, count in (([], 3), ([rate(0), rate(2)], 1)):
            with self.subTest(rows=len(rows)):
                result, _, _ = self.collect_rates(rows)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(sum(x["count"] for x in result["coverage"]["funding"]["missing_ranges"]), count)
                self.assertTrue(result["coverage"]["candles"]["complete"])

    def test_funding_identical_duplicates_are_removed(self):
        result, _, funding = self.collect_rates([rate(0), rate(1), rate(1), rate(2)])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(funding), 3)
        self.assertEqual(result["duplicates_removed"]["funding"], 1)

    def test_conflicts_and_distinct_records_in_same_hour_are_unavailable(self):
        for second in (rate(0, premium="0.1"), rate(0, time=rate(0)["time"] + 1)):
            with self.subTest(second=second):
                result, _, _ = self.collect_rates([rate(0), second])
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(result["issues"])

    def test_malformed_funding_and_page_bounds_are_unavailable(self):
        cases = [None, {}, rate(coin="BTC"), rate(time=True), rate(time=START - 1),
                 rate(time=START + 3 * HOUR), rate(fundingRate="NaN"), rate(premium="Infinity"),
                 rate(fundingRate=0.1), rate(premium=None)]
        for row in cases:
            with self.subTest(row=row):
                result, _, _ = self.collect_rates([row])
                self.assertEqual(result["status"], "unavailable")
        for rows in ({}, None, [rate()] * 501):
            with self.subTest(shape=type(rows).__name__):
                result, _, _ = self.collect_rates(rows)
                self.assertEqual(result["status"], "unavailable")

    def test_stalled_full_page_is_not_complete_and_does_not_loop(self):
        rates = [rate(i) for i in range(500)]
        reader = FixtureReader(META, [bar(i) for i in range(501)], rates, [rates[-1]] * 500)
        result, _, _ = dataset.collect(parameters(501, with_funding=True), reader)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("no timestamp progress", result["issues"][0])
        self.assertEqual(len(reader.records), 4)

    def test_request_budget_exhaustion_is_explicit_even_if_all_expected_hours_arrived(self):
        reader = FixtureReader(META, [bar(i) for i in range(500)], [rate(i) for i in range(500)])
        result, _, _ = dataset.collect(parameters(500, with_funding=True, max_pages=1), reader)
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["coverage"]["funding"]["complete"])
        self.assertIn("request budget exhausted", result["issues"][0])
        self.assertEqual(len(reader.records), 3)


class BundleTest(unittest.TestCase):
    def test_hashes_cover_stored_bytes_and_offline_replay_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_bundle(directory, parameters(with_funding=True), complete_reader(funding=True))
            for name, information in manifest["files"].items():
                content = (Path(directory) / name).read_bytes()
                self.assertEqual(information, {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
            with patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("offline replay opened network")):
                self.assertEqual(dataset.verify_bundle(directory), manifest)

    def test_tampered_csv_manifest_or_sources_cannot_verify(self):
        for name in ("candles.csv", "manifest.json", "sources.json", "funding.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                write_bundle(directory, parameters(with_funding=True), complete_reader(funding=True))
                path = Path(directory) / name
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaises(ValueError):
                    dataset.verify_bundle(directory)

    def test_recomputed_csv_hash_does_not_hide_divergence_from_source_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_bundle(directory)
            path = Path(directory) / "candles.csv"
            changed = path.read_bytes().replace(b"2000.000000000000000002", b"2000.500000000000000002")
            path.write_bytes(changed)
            manifest["files"]["candles.csv"] = {"bytes": len(changed), "sha256": hashlib.sha256(changed).hexdigest()}
            (Path(directory) / "manifest.json").write_bytes(dataset.json_bytes(manifest))
            with self.assertRaisesRegex(ValueError, "replayed evidence"):
                dataset.verify_bundle(directory)

    def test_missing_extra_and_symlinked_files_are_rejected(self):
        for change in ("missing", "extra", "symlink"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                write_bundle(directory)
                path = Path(directory) / "candles.csv"
                if change == "extra":
                    (Path(directory) / "extra.csv").write_text("unrelated")
                else:
                    contents = path.read_bytes()
                    path.unlink()
                    if change == "symlink":
                        (Path(directory) / "other.csv").write_bytes(contents)
                        path.symlink_to("other.csv")
                with self.assertRaises((ValueError, OSError)):
                    dataset.verify_bundle(directory)

    def test_captured_source_failure_replays_without_reinterpreting_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = FixtureReader(META, [bar(i) for i in range(3)], dataset.SourceError("fixture outage"))
            manifest = write_bundle(directory, parameters(with_funding=True), reader)
            self.assertEqual(manifest["result"]["status"], "unavailable")
            with patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("offline replay opened network")):
                replayed = dataset.verify_bundle(directory)
            self.assertEqual(replayed, manifest)

    def test_partial_funding_pages_are_preserved_when_later_read_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            rates = [rate(i) for i in range(500)]
            reader = FixtureReader(META, [bar(i) for i in range(501)], rates, dataset.SourceError("second page outage"))
            manifest = write_bundle(directory, parameters(501, with_funding=True), reader)
            self.assertEqual(manifest["result"]["status"], "unavailable")
            self.assertEqual(manifest["result"]["coverage"]["funding"]["observed"], 500)
            self.assertEqual(dataset.verify_bundle(directory), manifest)
            with (Path(directory) / "funding.csv").open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 500)

    def test_changed_and_unused_request_sequences_are_rejected(self):
        for change in ("wrong_request", "unused", "early_end"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                write_bundle(directory)
                source_path = Path(directory) / "sources.json"
                sources = json.loads(source_path.read_bytes())
                if change == "wrong_request":
                    sources["requests"][1]["request"]["req"]["endTime"] += 1
                elif change == "unused":
                    sources["requests"].append(copy.deepcopy(sources["requests"][-1]))
                else:
                    sources["requests"].pop()
                source_path.write_bytes(dataset.json_bytes(sources))
                with self.assertRaises(ValueError):
                    dataset.verify_bundle(directory)


class PublicReaderTest(unittest.TestCase):
    def response(self, raw):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw
        return response

    def test_only_unsigned_info_post_and_decoded_evidence_are_captured(self):
        response = self.response(b'{"universe": [{"name": "ETH"}]}')
        with patch.object(dataset.urllib.request, "urlopen", return_value=response) as opened:
            reader = dataset.PublicReader("https://api.hyperliquid-testnet.xyz", 7)
            self.assertEqual(reader({"type": "meta"}), META)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.hyperliquid-testnet.xyz/info")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"type": "meta"})
        self.assertEqual(opened.call_args.kwargs, {"timeout": 7})
        self.assertNotIn("Authorization", request.headers)
        response.read.assert_called_once_with(dataset.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(reader.records[0]["response"], META)
        self.assertEqual(reader.records[0]["request"], {"type": "meta"})
        self.assertTrue(reader.records[0]["retrieved_at_utc"].endswith("Z"))

    def test_public_reads_are_paced_and_do_not_add_unbounded_retry(self):
        response = self.response(b"[]")
        with patch.object(dataset.urllib.request, "urlopen", return_value=response) as opened, \
                patch.object(dataset.time, "monotonic", side_effect=[10.0, 11.0, 13.0]), \
                patch.object(dataset.time, "sleep") as slept:
            reader = dataset.PublicReader("https://api.hyperliquid.xyz", 15)
            reader({"type": "meta"})
            reader({"type": "fundingHistory", "coin": "ETH", "startTime": START})
        slept.assert_called_once_with(2.0)
        self.assertEqual(opened.call_count, 2)

    def test_transport_json_nonfinite_and_response_size_failures_are_recorded(self):
        cases = [OSError("sensitive upstream details"), b"bad JSON", b'{"hidden": NaN}', b"x" * 33]
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                kwargs = {"side_effect": payload} if isinstance(payload, Exception) else {"return_value": self.response(payload)}
                with patch.object(dataset.urllib.request, "urlopen", **kwargs) as opened, \
                        patch.object(dataset, "MAX_RESPONSE_BYTES", 32):
                    reader = dataset.PublicReader("https://api.hyperliquid.xyz", 15)
                    with self.assertRaises(dataset.SourceError):
                        reader({"type": "meta"})
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(len(reader.records), 1)
                self.assertIn("error", reader.records[0])
                self.assertNotIn("response", reader.records[0])
                self.assertNotIn("sensitive", reader.records[0]["error"])

    def test_real_reader_captured_transport_failure_can_be_verified_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = dataset.PublicReader("https://api.hyperliquid.xyz", 15)
            with patch.object(dataset.urllib.request, "urlopen", side_effect=OSError("fixture outage")):
                manifest = write_bundle(directory, parameters(), reader)
            self.assertEqual(manifest["result"]["status"], "unavailable")
            with patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("replay opened network")):
                self.assertEqual(dataset.verify_bundle(directory), manifest)

    def test_bad_http_status_line_is_captured_and_sanitized_without_retry(self):
        reader = dataset.PublicReader("https://api.hyperliquid.xyz", 15)
        with patch.object(dataset.urllib.request, "urlopen", side_effect=BadStatusLine("sensitive upstream details")) as opened:
            with self.assertRaisesRegex(dataset.SourceError, "BadStatusLine: public /info read failed"):
                reader({"type": "meta"})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(reader.records), 1)
        self.assertTrue(reader.records[0]["error"].startswith("BadStatusLine: public /info read failed"))
        self.assertNotIn("response", reader.records[0])
        self.assertNotIn("sensitive", json.dumps(reader.records))

    def test_truncated_later_funding_response_saves_partial_cli_bundle_and_replays_offline(self):
        rates = [rate(i) for i in range(500)]
        partial = self.response(b"")
        partial.read.side_effect = IncompleteRead(b"sensitive upstream details", 1024)
        responses = [self.response(dataset.json_bytes(META)),
                     self.response(dataset.json_bytes([bar(i) for i in range(501)])),
                     self.response(dataset.json_bytes(rates)), partial]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "partial-bundle"
            args = ["--coin", "ETH", "--interval", "1h", "--start", "2024-01-01T00:00:00Z",
                    "--end", "2024-01-21T21:00:00Z", "--with-funding", "--output", str(output), "--json"]
            with patch.object(dataset.urllib.request, "urlopen", side_effect=responses) as opened, \
                    patch.object(dataset.time, "time", return_value=(START + 502 * HOUR) / 1000), \
                    patch.object(dataset.time, "monotonic", return_value=0), \
                    patch.object(dataset.time, "sleep") as slept, \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(dataset.main(args), 1)
            self.assertEqual(opened.call_count, 4)
            self.assertEqual(slept.call_count, 3)
            self.assertEqual(err.getvalue(), "")
            manifest = json.loads(out.getvalue())
            self.assertEqual(manifest["result"]["status"], "unavailable")
            self.assertEqual(manifest["result"]["coverage"]["candles"]["observed"], 501)
            self.assertTrue(manifest["result"]["coverage"]["candles"]["complete"])
            self.assertEqual(manifest["result"]["coverage"]["funding"]["observed"], 500)
            self.assertEqual(len(manifest["result"]["issues"]), 1)
            self.assertTrue(manifest["result"]["issues"][0].startswith("IncompleteRead: public /info read failed"))
            self.assertEqual(json.loads((output / "manifest.json").read_bytes()), manifest)
            with (output / "candles.csv").open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 501)
            with (output / "funding.csv").open(newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 500)
            sources = json.loads((output / "sources.json").read_bytes())
            self.assertEqual(len(sources["requests"]), 4)
            failure = sources["requests"][-1]
            self.assertEqual(failure["request"]["startTime"], rates[-1]["time"])
            self.assertEqual(failure["error"], manifest["result"]["issues"][0])
            self.assertNotIn("response", failure)
            self.assertNotIn("sensitive", (output / "sources.json").read_text())
            with patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("offline replay opened network")), \
                    patch.object(dataset, "PublicReader", side_effect=AssertionError("offline replay constructed reader")), \
                    redirect_stdout(io.StringIO()) as verified:
                self.assertEqual(dataset.verify_bundle(output), manifest)
                self.assertEqual(dataset.main(["--verify", str(output), "--json"]), 1)
            replay = json.loads(verified.getvalue())
            self.assertEqual(replay["verification"], "verified")
            self.assertEqual(replay["manifest"], manifest)


class CommandLineTest(unittest.TestCase):
    def args(self, output):
        return ["--coin", "ETH", "--interval", "1h", "--start", "2024-01-01T00:00:00Z",
                "--end", "2024-01-01T03:00:00Z", "--output", str(output), "--json"]

    def test_schema_never_constructs_a_reader_or_opens_network(self):
        with patch.object(dataset, "PublicReader", side_effect=AssertionError("reader constructed")), \
                patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("network opened")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dataset.main(["--schema"]), 0)
        schema = json.loads(out.getvalue())
        self.assertEqual(schema["mode"], "read-only")
        self.assertEqual(schema["exit_codes"]["2"], "invalid arguments")

    def test_invalid_arguments_do_not_read_or_create_output(self):
        cases = [["--coin", "@107"], ["--coin", "xyz:ETH", "--dex", "abc"],
                 ["--start", "2024-01-01T00:00:01Z"], ["--end", "2024-01-01T00:00:00Z"],
                 ["--end", "2099-01-01T03:00:00Z"], ["--start", "2024-01-01"],
                 ["--max-pages", "0"], ["--timeout", "nan"], ["--timeout", "0"],
                 ["--base-url", "https://example.com/exchange"], ["--interval", "1M"],
                 ["--interval", "1m", "--end", "2024-01-01T00:01:00Z", "--with-funding"]]
        for extra in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "new-bundle"
                with patch.object(dataset, "PublicReader", side_effect=AssertionError("reader constructed")), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as caught:
                        dataset.main(self.args(output) + extra)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(json.loads(out.getvalue())["error"]["code"], "invalid_arguments")
                self.assertEqual(err.getvalue(), "")
                self.assertFalse(output.exists())

    def test_collection_writes_verifiable_bundle_and_refuses_overwrite_before_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-bundle"
            with patch.object(dataset, "PublicReader", return_value=complete_reader()), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(dataset.main(self.args(output)), 0)
            manifest = json.loads(out.getvalue())
            self.assertEqual(dataset.verify_bundle(output), manifest)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with patch.object(dataset, "PublicReader", side_effect=AssertionError("overwrite opened reader")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    dataset.main(self.args(output))
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)

    def test_collection_and_offline_verification_exit_codes_follow_evidence_status(self):
        for status, reader in (("complete", complete_reader()),
                               ("incomplete", FixtureReader(META, [])),
                               ("unavailable", FixtureReader(dataset.SourceError("fixture outage")))):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "new-bundle"
                expected = 0 if status == "complete" else 1
                with patch.object(dataset, "PublicReader", return_value=reader), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(dataset.main(self.args(output)), expected)
                self.assertEqual(json.loads(out.getvalue())["result"]["status"], status)
                with patch.object(dataset, "PublicReader", side_effect=AssertionError("offline reader constructed")), \
                        patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("offline network opened")), \
                        redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(dataset.main(["--verify", str(output), "--json"]), expected)
                verified = json.loads(out.getvalue())
                self.assertEqual(verified["verification"], "verified")
                self.assertEqual(verified["manifest"]["result"]["status"], status)

    def test_verify_rejects_overrides_and_reports_missing_bundle_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(dataset, "PublicReader", side_effect=AssertionError("offline reader constructed")), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(dataset.main(["--verify", str(Path(directory) / "missing"), "--json"]), 1)
            self.assertEqual(json.loads(out.getvalue())["error"]["code"], "verification_failed")
            write_bundle(directory)
            with patch.object(dataset, "PublicReader", side_effect=AssertionError("offline reader constructed")), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    dataset.main(["--verify", directory, "--json", "--coin", "BTC"])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(json.loads(out.getvalue())["error"]["code"], "invalid_arguments")

    def test_malformed_captured_parameters_return_structured_offline_verification_error(self):
        for field in ("base_url", "network", "interval", "dex"):
            for malformed in (True, {"unexpected": "object"}, ["unexpected-list"]):
                with self.subTest(field=field, malformed=malformed), tempfile.TemporaryDirectory() as directory:
                    write_bundle(directory)
                    source_path = Path(directory) / "sources.json"
                    sources = json.loads(source_path.read_bytes())
                    sources["parameters"][field] = malformed
                    source_path.write_bytes(dataset.json_bytes(sources))
                    with patch.object(dataset.urllib.request, "urlopen", side_effect=AssertionError("verify opened network")), \
                            patch.object(dataset, "PublicReader", side_effect=AssertionError("verify constructed reader")), \
                            redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(dataset.main(["--verify", directory, "--json"]), 1)
                    failure = json.loads(out.getvalue())
                    self.assertEqual(failure["mode"], "read-only")
                    self.assertEqual(failure["error"]["code"], "verification_failed")
                    self.assertIsInstance(failure["error"]["message"], str)
                    self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
