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
requests = []
failures = []


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append((self.path, payload))
        responses = {"meta": META, "metaAndAssetCtxs": [META, [CONTEXT]], "l2Book": BOOK, "candleSnapshot": CANDLES}
        if self.path != "/info" or payload.get("type") not in responses:
            failures.append("Unexpected request outside the read-only /info contract")
            self.send_error(400)
            return
        body = json.dumps(responses[payload["type"]]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def command(name, *args, expected=0):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py"), *args],
                            cwd=ROOT, env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"},
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
        for name in ("opening_bell", "stop_cost", "desk_doctor", "research_dataset"):
            schema = command(name, "--schema", "--base-url", base)
            assert schema["mode"] == "read-only" and schema["schema_version"] == 1
            invalid = command(name, "--json", "--timeout", "nan", "--base-url", base, expected=2)
            assert invalid["mode"] == "read-only" and invalid["error"]["code"] == "invalid_arguments"
            checked += 2
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
        print(f"Passed {checked} read-only CLI journeys: schemas, invalid input, offline doctor, market read, risk sizing, dataset collection/replay/tamper refusal; only loopback POST /info used.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
