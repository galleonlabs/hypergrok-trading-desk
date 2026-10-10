#!/usr/bin/env python3
"""Collect and verify public Hyperliquid research datasets; never signs or trades."""
from __future__ import annotations

import csv
import hashlib
from http.client import HTTPException
import io
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from cli_contract import ReadOnlyParser, add_network_arguments, emit_error, network_settings

HOUR = 3_600_000
INTERVALS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
             "30m": 1_800_000, "1h": HOUR, "2h": 2 * HOUR, "4h": 4 * HOUR,
             "8h": 8 * HOUR, "12h": 12 * HOUR, "1d": 24 * HOUR}
CANDLE_FIELDS = ("t", "T", "s", "i", "o", "h", "l", "c", "v", "n")
FUNDING_FIELDS = ("time", "coin", "fundingRate", "premium")
SCHEMA_VERSION = 1
PAGE_SIZE = 500
MAX_RESPONSE_BYTES = 10_000_000


class SourceError(ValueError):
    pass


def utc(ms=None):
    value = datetime.now(timezone.utc) if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise ValueError("times must use UTC YYYY-MM-DDTHH:MM:SSZ")
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()) * 1000


def integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def number(value, name, positive=False, nonnegative=False):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError(f"{name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{name} has an invalid numeric value")
    return result


def validate_parameters(p):
    if not isinstance(p, dict) or set(p) != {"coin", "dex", "interval", "start_ms", "end_ms", "as_of_ms", "with_funding", "max_pages", "base_url", "network"}:
        raise ValueError("dataset parameters have an unexpected shape")
    coin, dex = p["coin"], p["dex"]
    if not isinstance(coin, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?", coin):
        raise ValueError("coin must name a perpetual market, optionally prefixed with its DEX")
    if dex is not None and (not isinstance(dex, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", dex)):
        raise ValueError("dex must be a lowercase DEX name")
    if ":" in coin and coin.split(":")[0] != dex:
        raise ValueError("coin prefix and dex must match")
    if p["interval"] not in INTERVALS:
        raise ValueError("unsupported fixed candle interval")
    start, end = integer(p["start_ms"], "start_ms"), integer(p["end_ms"], "end_ms")
    as_of = integer(p["as_of_ms"], "as_of_ms")
    step = INTERVALS[p["interval"]]
    if start >= end or start % step or end % step:
        raise ValueError("start and end must be increasing UTC candle boundaries; end is exclusive")
    if end > as_of:
        raise ValueError("end must exclude the current unfinished candle and cannot be in the future")
    if (end - start) // step > 5000:
        raise ValueError("request at most 5000 candles; the API retains only the most recent 5000")
    if type(p["with_funding"]) is not bool:
        raise ValueError("with_funding must be a boolean")
    if p["with_funding"] and (start % HOUR or end % HOUR):
        raise ValueError("with-funding requires UTC hour boundaries")
    if type(p["max_pages"]) is not int or not 1 <= p["max_pages"] <= 250:
        raise ValueError("max-pages must be between 1 and 250")
    if not isinstance(p["base_url"], str) or not isinstance(p["network"], str):
        raise ValueError("source origin and network must be strings")
    # Use the same origin validation as the other read-only tools, without CLI side effects.
    from argparse import Namespace
    class Validator:
        @staticmethod
        def error(message):
            raise ValueError(message)
    base, network = network_settings(Namespace(timeout=15.0, base_url=p["base_url"], testnet=False), Validator())
    if base != p["base_url"] or network != p["network"]:
        raise ValueError("source origin and network label disagree")


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def csv_bytes(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PublicReader:
    """Paced public reads with the exact decoded response and request retained."""
    def __init__(self, base_url, timeout):
        self.base_url, self.timeout = base_url, timeout
        self.records = []
        self.last_request = None

    def __call__(self, payload):
        if self.last_request is not None:
            time.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        record = {"request": payload}
        self.records.append(record)
        request = urllib.request.Request(self.base_url + "/info", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "hypergrok-research-dataset/1"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("public response exceeded 10 MB")
            result = json.loads(raw)
            # Reject non-standard JSON numeric values even if hidden in unused metadata.
            json_bytes(result)
        except (OSError, ValueError, HTTPException) as exc:
            status = f" {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            record["retrieved_at_utc"] = utc()
            record["error"] = f"{type(exc).__name__}{status}: public /info read failed; no retry attempted"
            raise SourceError(record["error"]) from exc
        record["retrieved_at_utc"] = utc()
        record["response"] = result
        return result


class ReplayReader:
    """Consumes only captured responses, and enforces the same request sequence."""
    def __init__(self, records):
        if not isinstance(records, list):
            raise ValueError("sources requests must be a list")
        self.records, self.index = records, 0

    def __call__(self, payload):
        if self.index >= len(self.records):
            raise ValueError("captured request sequence ended early")
        record = self.records[self.index]
        self.index += 1
        if not isinstance(record, dict) or record.get("request") != payload:
            raise ValueError("captured request differs from the required request")
        if ("response" in record) == ("error" in record):
            raise ValueError("captured request must contain either response or error")
        if not isinstance(record.get("retrieved_at_utc"), str):
            raise ValueError("captured request lacks retrieval time")
        if "error" in record:
            if not isinstance(record["error"], str):
                raise ValueError("captured error must be text")
            raise SourceError(record["error"])
        return record["response"]


def resolve_market(meta, p):
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in universe):
        raise ValueError("meta must contain a named perpetual universe")
    target = f'{p["dex"]}:{p["coin"]}' if p["dex"] and ":" not in p["coin"] else p["coin"]
    matches = [x for x in universe if x["name"].casefold() == target.casefold()]
    if len(matches) != 1:
        raise ValueError("coin is not a unique market in the selected perpetual universe")
    asset = matches[0]
    if p["dex"] and not asset["name"].startswith(p["dex"] + ":"):
        raise ValueError("market does not belong to the selected DEX")
    if type(asset.get("isDelisted", False)) is not bool:
        raise ValueError("isDelisted must be boolean")
    return {"coin": asset["name"], "dex": p["dex"], "is_delisted": asset.get("isDelisted", False)}


def candle(row, coin, interval):
    if not isinstance(row, dict) or not all(k in row for k in CANDLE_FIELDS):
        raise ValueError("candle is missing required fields")
    if row["s"] != coin or row["i"] != interval:
        raise ValueError("candle coin or interval differs from the request")
    t, end = integer(row["t"], "candle t"), integer(row["T"], "candle T")
    step = INTERVALS[interval]
    if t % step or end != t + step - 1:
        raise ValueError("candle times do not describe one aligned interval")
    o, h, low, c = [number(row[k], k, positive=True) for k in ("o", "h", "l", "c")]
    if not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError("candle OHLC prices are inconsistent")
    number(row["v"], "volume", nonnegative=True)
    integer(row["n"], "trade count")
    return {k: row[k] for k in CANDLE_FIELDS}


def funding_row(row, coin):
    if not isinstance(row, dict) or not all(k in row for k in FUNDING_FIELDS):
        raise ValueError("funding record is missing required fields")
    if row["coin"] != coin:
        raise ValueError("funding coin differs from the request")
    integer(row["time"], "funding time")
    number(row["fundingRate"], "fundingRate")
    number(row["premium"], "premium")
    return {k: row[k] for k in FUNDING_FIELDS}


def insert_unique(rows, row, key):
    stamp = row[key]
    if stamp in rows:
        if rows[stamp] != row:
            raise ValueError(f"conflicting records at {key}={stamp}")
        return 1
    rows[stamp] = row
    return 0


def coverage(rows, start, end, step):
    """Ranges describe missing slots, not guesses about why they are missing."""
    missing, pending = [], None
    for stamp in range(start, end, step):
        if stamp not in rows and pending is None:
            pending = stamp
        elif stamp in rows and pending is not None:
            missing.append({"start_ms": pending, "end_ms": stamp, "count": (stamp - pending) // step})
            pending = None
    if pending is not None:
        missing.append({"start_ms": pending, "end_ms": end, "count": (end - pending) // step})
    return {"expected": (end - start) // step, "observed": len(rows), "missing_ranges": missing,
            "complete": not missing, "first_ms": min(rows) if rows else None, "last_ms": max(rows) if rows else None}


def collect(p, read):
    """One bounded collection algorithm for both live reads and offline replay."""
    validate_parameters(p)
    candles, funding = {}, {}
    result = {"status": "complete", "market": None, "issues": [], "duplicates_removed": {"candles": 0, "funding": 0},
              "candles_excluded_outside_window": 0}
    start, end = p["start_ms"], p["end_ms"]
    try:
        meta_request = {"type": "meta"}
        if p["dex"]:
            meta_request["dex"] = p["dex"]
        result["market"] = resolve_market(read(meta_request), p)
        coin = result["market"]["coin"]
        batch = read({"type": "candleSnapshot", "req": {"coin": coin, "interval": p["interval"], "startTime": start, "endTime": end - 1}})
        if not isinstance(batch, list) or len(batch) > 10_002:
            raise ValueError("candleSnapshot must return a bounded list")
        for item in batch:
            row = candle(item, coin, p["interval"])
            if not start <= row["t"] < end or row["T"] >= p["as_of_ms"]:
                result["candles_excluded_outside_window"] += 1
                continue
            result["duplicates_removed"]["candles"] += insert_unique(candles, row, "t")
        if p["with_funding"]:
            cursor = start
            hours = {}
            for page in range(p["max_pages"]):
                batch = read({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end - 1})
                if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
                    raise ValueError("fundingHistory must return at most 500 records")
                latest = cursor
                for item in batch:
                    row = funding_row(item, coin)
                    stamp = row["time"]
                    if not cursor <= stamp < end:
                        raise ValueError("funding time is outside the requested page")
                    bucket = stamp // HOUR * HOUR
                    if bucket in hours and hours[bucket] != stamp:
                        raise ValueError("multiple distinct funding records in one UTC hour")
                    hours[bucket] = stamp
                    result["duplicates_removed"]["funding"] += insert_unique(funding, row, "time")
                    latest = max(latest, stamp)
                if len(batch) < PAGE_SIZE:
                    break
                if latest <= cursor:
                    raise ValueError("funding pagination made no timestamp progress")
                cursor = latest  # Inclusive overlap preserves the exact exchange timestamp.
            else:
                result["issues"].append("funding request budget exhausted; final page may be truncated")
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result["status"] = "unavailable"
        result["issues"].append(str(exc))
    result["coverage"] = {"candles": coverage(candles, start, end, INTERVALS[p["interval"]]),
                          "funding": coverage({t // HOUR * HOUR for t in funding}, start, end, HOUR) if p["with_funding"] else None}
    if result["status"] != "unavailable" and (result["issues"] or any(c and not c["complete"] for c in result["coverage"].values())):
        result["status"] = "incomplete"
    return result, [candles[t] for t in sorted(candles)], [funding[t] for t in sorted(funding)]


def bundle_content(p, records, result, candles, funding):
    files = {"sources.json": json_bytes({"schema_version": SCHEMA_VERSION, "parameters": p, "requests": records}),
             "candles.csv": csv_bytes(candles, CANDLE_FIELDS)}
    if p["with_funding"]:
        files["funding.csv"] = csv_bytes(funding, FUNDING_FIELDS)
    manifest = {"schema_version": SCHEMA_VERSION, "mode": "read-only", "parameters": p, "result": result,
                "files": {name: {"sha256": digest(data), "bytes": len(data)} for name, data in files.items()},
                "limitations": ["Hashes prove local consistency, not exchange authenticity or an unchanged provider history.",
                                "Complete coverage is not strategy validation, Risk PASS, approval, or execution readiness.",
                                "Funding rates alone cannot prove dollar funding costs without historical oracle prices and position sizes.",
                                "Current metadata is not a survivorship-free universe; absent markets remain unavailable."]}
    files["manifest.json"] = json_bytes(manifest)
    return manifest, files


def verify_bundle(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("bundle directory does not exist")
    # Fixed names only; never follow a manifest-supplied path or execute captured code.
    for name in ("sources.json", "manifest.json", "candles.csv", "funding.csv"):
        if (directory / name).is_symlink():
            raise ValueError(f"bundle file must not be a symlink: {name}")
    sources = json.loads((directory / "sources.json").read_bytes())
    if not isinstance(sources, dict) or sources.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported sources schema")
    replay = ReplayReader(sources["requests"])
    p = sources["parameters"]
    result, candles, funding = collect(p, replay)
    if replay.index != len(replay.records):
        raise ValueError("bundle contains unused requests")
    manifest, expected = bundle_content(p, replay.records, result, candles, funding)
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != set(expected):
        raise ValueError("bundle file inventory differs from the expected evidence files")
    for name, content in expected.items():
        if (directory / name).read_bytes() != content:
            raise ValueError(f"bundle differs from replayed evidence: {name}")
    return manifest


def schema():
    return {"schema_version": SCHEMA_VERSION, "command": "research_dataset", "mode": "read-only",
            "network_access": "Paced public POST /info: meta, candleSnapshot, fundingHistory only; none for --schema or --verify",
            "intervals": list(INTERVALS), "window": "Explicit UTC [start,end), aligned closed candles, at most 5000 bars; funding requires whole hours",
            "files": ["manifest.json", "sources.json", "candles.csv", "funding.csv (only with --with-funding)"],
            "status": "complete | incomplete | unavailable; coverage is assessed for the exact requested window",
            "exit_codes": {"0": "complete dataset; for --verify also verified local consistency", "1": "incomplete, unavailable, output failure or failed verification", "2": "invalid arguments"},
            "success": "manifest with parameters, result (status, market, issues, coverage, duplicates), file SHA-256 and sizes, limitations",
            "error": {"schema_version": 1, "mode": "read-only", "error": {"code": "invalid_arguments | output_unavailable | verification_failed", "message": "string"}}}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = ReadOnlyParser(description=__doc__, machine_readable="--json" in argv)
    parser.add_argument("--coin")
    parser.add_argument("--dex")
    parser.add_argument("--interval", choices=INTERVALS)
    parser.add_argument("--start", help="UTC YYYY-MM-DDTHH:MM:SSZ, inclusive")
    parser.add_argument("--end", help="UTC YYYY-MM-DDTHH:MM:SSZ, exclusive, closed candles only")
    parser.add_argument("--with-funding", action="store_true")
    parser.add_argument("--max-pages", type=int, default=100, help="funding request budget (1..250)")
    parser.add_argument("--output", type=Path, help="new bundle directory; existing paths are never overwritten")
    parser.add_argument("--verify", type=Path, help="verify a saved bundle offline by replaying its captured requests")
    parser.add_argument("--json", action="store_true")
    add_network_arguments(parser)
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(schema(), indent=2, sort_keys=True))
        return 0
    if args.verify:
        allowed = {"--verify", "--json"}
        if any(token.startswith("--") and token.split("=")[0] not in allowed for token in argv):
            parser.error("--verify accepts only --json; it uses the bundle's captured parameters")
        try:
            manifest = verify_bundle(args.verify)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            if args.json:
                emit_error("verification_failed", str(exc))
            else:
                print(f"Dataset verification failed: {exc}", file=sys.stderr)
            return 1
        output = {"schema_version": SCHEMA_VERSION, "mode": "read-only", "verification": "verified", "manifest": manifest}
        print(json.dumps(output, indent=2, sort_keys=True) if args.json else f'Verified local evidence: {args.verify} | {manifest["result"]["status"]}')
        return 0 if manifest["result"]["status"] == "complete" else 1
    for name in ("coin", "interval", "start", "end", "output"):
        if getattr(args, name) is None:
            parser.error(f"--{name} is required for collection")
    base, network = network_settings(args, parser)
    try:
        p = {"coin": args.coin, "dex": args.dex or (args.coin.split(":")[0] if ":" in args.coin else None),
             "interval": args.interval, "start_ms": parse_utc(args.start), "end_ms": parse_utc(args.end),
             "as_of_ms": int(time.time() * 1000), "with_funding": args.with_funding, "max_pages": args.max_pages,
             "base_url": base, "network": network}
        validate_parameters(p)
    except (ValueError, TypeError, OverflowError) as exc:
        parser.error(str(exc))
    # Reserve the destination before any reads; concurrent collectors cannot overwrite one another.
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(f"output must be a new writable directory: {exc}")
    reader = PublicReader(base, args.timeout)
    result, candles, funding = collect(p, reader)
    manifest, files = bundle_content(p, reader.records, result, candles, funding)
    try:
        # The manifest is the completion marker, written last. Interruptions leave no valid bundle.
        for name, data in files.items():
            with (args.output / name).open("xb") as handle:
                handle.write(data)
    except OSError as exc:
        if args.json:
            emit_error("output_unavailable", str(exc))
        else:
            print(f"Dataset output unavailable: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        counts = result["coverage"]
        print(f'Dataset {result["status"]}: {args.output}\nCandles: {counts["candles"]["observed"]}/{counts["candles"]["expected"]}')
        if counts["funding"]:
            print(f'Funding hours: {counts["funding"]["observed"]}/{counts["funding"]["expected"]}')
        for issue in result["issues"]:
            print(f"Unavailable: {issue}")
        print("Inspect manifest.json for missing ranges and limitations; local integrity is not a trading approval.")
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
