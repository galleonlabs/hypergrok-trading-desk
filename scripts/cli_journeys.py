#!/usr/bin/env python3
"""Exercise the published read-only commands as subprocesses against synthetic loopback data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
START = 1704067200000
META = {"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]}
CONTEXT = {"markPx": "3001", "oraclePx": "3000", "prevDayPx": "2950", "funding": "0", "openInterest": "100", "dayNtlVlm": "5000000"}
BOOK = {"coin": "ETH", "time": START, "levels": [
    [{"px": "3000", "sz": "100", "n": 1}, {"px": "2900", "sz": "100", "n": 1}],
    [{"px": "3001", "sz": "100", "n": 1}, {"px": "3100", "sz": "100", "n": 1}],
]}
CANDLES = [{"t": START + i * 3600000, "T": START + (i + 1) * 3600000 - 1, "s": "ETH", "i": "1h",
            "o": "3000", "h": "3002", "l": "2999", "c": "3001", "v": "5", "n": 2} for i in range(3)]
STANDARD = "0x" + "1" * 40
UNIFIED = "0x" + "2" * 40


def state(value, positions=()):
    return {"marginSummary": {"accountValue": value, "totalNtlPos": "0.0", "totalRawUsd": value, "totalMarginUsed": "0.0"},
            "crossMarginSummary": {"accountValue": value}, "crossMaintenanceMarginUsed": "0.0", "withdrawable": value,
            "assetPositions": [{"type": "oneWay", "position": position} for position in positions], "time": START}


# Synthetic account on the main DEX, a USDC HIP-3 DEX with a position, a USDH-margined DEX and an empty DEX.
PERP_DEXS = [None, {"name": "xyz", "fullName": "XYZ"}, {"name": "flx", "fullName": "Felix"}, {"name": "vntl", "fullName": "Ventuals"}]
ACCOUNT_STATES = {
    "": state("1000.0", [{"coin": "ETH", "szi": "0.1", "entryPx": "3000.0", "positionValue": "300.1", "unrealizedPnl": "0.1",
                          "marginUsed": "15.0", "leverage": {"type": "cross", "value": 20}, "liquidationPx": None}]),
    "xyz": state("500.0", [{"coin": "xyz:TSLA", "szi": "-2.0", "entryPx": "250.0", "positionValue": "490.0", "unrealizedPnl": "10.0",
                            "marginUsed": "49.0", "leverage": {"type": "isolated", "value": 10, "rawUsd": "549.0"}, "liquidationPx": "300.0"}]),
    "flx": state("200.0"),
    "vntl": state("0.0"),
}
DEX_COLLATERAL = {"": 0, "xyz": 0, "flx": 360, "vntl": 0}
SPOT_META = [{"tokens": [{"name": "USDC", "index": 0}, {"name": "HYPE", "index": 150}, {"name": "USDH", "index": 360}, {"name": "JUNK", "index": 999}],
              "universe": [{"tokens": [150, 0], "name": "@107", "index": 107}, {"tokens": [360, 0], "name": "@230", "index": 230}]},
             [{"coin": "@107", "midPx": "40.0", "markPx": "39.0"}, {"coin": "@230", "midPx": None, "markPx": "0.999"}]]
BALANCES = [{"coin": "USDC", "token": 0, "total": "100.0", "hold": "10.0", "entryNtl": "0.0"},
            {"coin": "HYPE", "token": 150, "total": "2.0", "hold": "0.0", "entryNtl": "70.0"},
            {"coin": "PURR", "token": 1, "total": "0.0", "hold": "0.0", "entryNtl": "0.0"}]
requests = []
failures = []


def account_response(payload):
    user = payload.get("user")
    if payload["type"] == "perpDexs":
        return PERP_DEXS
    if payload["type"] == "spotMetaAndAssetCtxs":
        return SPOT_META
    if user not in (STANDARD, UNIFIED):
        return None
    if payload["type"] == "userAbstraction":
        return "default" if user == STANDARD else "unifiedAccount"
    if payload["type"] == "clearinghouseState":
        return ACCOUNT_STATES.get(payload.get("dex", ""))
    extra = [] if user == STANDARD else [{"coin": "JUNK", "token": 999, "total": "5.0", "hold": "0.0", "entryNtl": "1.0"}]
    return {"balances": BALANCES + extra}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append((self.path, payload))
        responses = {"meta": META, "metaAndAssetCtxs": [META, [CONTEXT]], "l2Book": BOOK, "candleSnapshot": CANDLES}
        kind = payload.get("type")
        if kind == "meta" and "dex" in payload:
            response = {**META, "collateralToken": DEX_COLLATERAL[payload["dex"]]} if payload["dex"] in DEX_COLLATERAL else None
        elif kind in ("perpDexs", "userAbstraction", "clearinghouseState", "spotClearinghouseState", "spotMetaAndAssetCtxs"):
            response = account_response(payload)
        else:
            response = responses.get(kind)
        if self.path != "/info" or response is None:
            failures.append("Unexpected request outside the read-only /info contract")
            self.send_error(400)
            return
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def command(name, *args, expected=0, env=None):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py"), *args],
                            cwd=ROOT, env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1", **(env or {})},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == expected, f"{name}: exit {result.returncode}; {result.stderr}; {result.stdout}"
    return json.loads(result.stdout)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    checked = 0
    try:
        for name in ("opening_bell", "stop_cost", "desk_doctor", "research_dataset", "account_read"):
            schema = command(name, "--schema", "--base-url", base)
            assert schema["mode"] == "read-only" and schema["schema_version"] == 1
            invalid = command(name, "--json", "--timeout", "nan", "--base-url", base, expected=2)
            assert invalid["mode"] == "read-only" and invalid["error"]["code"] == "invalid_arguments"
            checked += 2
        missing = command("account_read", "--json", "--base-url", base, expected=2)
        assert missing["error"]["code"] == "invalid_arguments" and "HYPERGROK_ACCOUNT" in missing["error"]["message"]
        checked += 1
        assert not requests, "Schema and invalid requests must not access any API"
        doctor = command("desk_doctor", "--offline", "--json", "--base-url", base)
        assert all(check["status"] != "FAIL" for check in doctor["checks"])
        assert any(check["name"] == "public API" and check["status"] == "SKIP" for check in doctor["checks"])
        assert not requests, "Offline doctor must not access any API"
        checked += 1
        bell = command("opening_bell", "--coin", "ETH", "--json", "--base-url", base)
        assert bell["mode"] == "read-only" and bell["coin"] == "ETH" and bell["network"] == "custom"
        assert bell["prices"]["mark"] == "3001"
        checked += 1
        stop = command("stop_cost", "--coin", "ETH", "--entry", "3000", "--stop", "2900", "--equity", "10000", "--risk-pct", "0.5", "--json", "--base-url", base)
        assert stop["mode"] == "read-only" and stop["side"] == "long"
        assert Decimal(stop["stressed_stop"]["loss_if_triggered"]) <= Decimal("50")
        assert Decimal(stop["stressed_stop"]["size"]) < Decimal(stop["drawn_stop"]["size"])
        checked += 1
        count = len(requests)
        account = command("account_read", "--address", STANDARD, "--json", "--base-url", base)
        account_requests = requests[count:]
        assert account["mode"] == "read-only" and account["abstraction"] == "default"
        assert [dex["label"] for dex in account["dexes"]] == ["main", "xyz", "flx", "vntl"]
        tsla = account["dexes"][1]["positions"][0]
        assert (tsla["coin"], tsla["side"], tsla["size"], tsla["liquidation_px"]) == ("xyz:TSLA", "short", "2.0", "300.0")
        assert account["dexes"][2]["collateral"] == "USDH" and Decimal(account["dexes"][2]["account_value_usd"]) == Decimal("199.8")
        assert account["dexes"][3]["collateral"] is None and not account["dexes"][3]["positions"]
        equity = account["equity"]
        assert equity["basis"] == "separate" and equity["complete"] and not equity["unpriced"]
        assert Decimal(equity["perp_usd"]) == Decimal("1699.8") and Decimal(equity["spot_usd"]) == Decimal("180")
        assert Decimal(equity["total_usd"]) == Decimal("1879.8")
        assert {"type": "clearinghouseState", "user": STANDARD, "dex": "xyz"} in [payload for _path, payload in account_requests]
        assert {"type": "meta", "dex": "vntl"} not in [payload for _path, payload in account_requests], "Empty DEX metadata is not needed"
        checked += 1
        shared = command("account_read", "--json", "--base-url", base, env={"HYPERGROK_ACCOUNT": UNIFIED})
        assert shared["abstraction"] == "unifiedAccount" and shared["equity"]["basis"] == "shared"
        assert shared["equity"]["total_usd"] is None and not shared["equity"]["complete"]
        assert shared["equity"]["unpriced"] == ["spot JUNK"] and len(shared["dexes"][1]["positions"]) == 1
        checked += 1
        with tempfile.TemporaryDirectory(prefix="hypergrok-cli-") as temporary:
            bundle = Path(temporary) / "bundle"
            result = command("research_dataset", "--coin", "ETH", "--interval", "1h", "--start", "2024-01-01T00:00:00Z", "--end", "2024-01-01T03:00:00Z", "--output", str(bundle), "--json", "--base-url", base)
            assert result["result"]["status"] == "complete" and result["result"]["coverage"]["candles"]["observed"] == 3
            count = len(requests)
            verified = command("research_dataset", "--verify", str(bundle), "--json")
            assert verified["verification"] == "verified"
            assert len(requests) == count, "Bundle verification must be offline"
            candles = bundle / "candles.csv"
            assert candles.exists()
            candles.write_text(candles.read_text().replace("3001", "3002", 1))
            tampered = command("research_dataset", "--verify", str(bundle), "--json", expected=1)
            assert tampered["error"]["code"] == "verification_failed"
            checked += 3
        assert not failures, failures
        print(f"Passed {checked} read-only CLI journeys: schemas, invalid input, offline doctor, market read, every-DEX account read, risk sizing, dataset collection/replay/tamper refusal; only loopback POST /info used.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
