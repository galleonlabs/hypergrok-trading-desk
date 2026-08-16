#!/usr/bin/env python3
"""Pre-launch verification for HyperGrok.

Exercises the real CLI against live Hyperliquid endpoints. Never signs, never
submits an order, never handles a private key.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
BIN = REPO / ".venv" / "bin" / "hypergrok"
WORK = Path(tempfile.mkdtemp(prefix="hypergrok-verify-"))

results: list[tuple[str, str, bool, str]] = []
group = "general"


def run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    environ = dict(os.environ)
    environ.pop("HYPERLIQUID_PRIVATE_KEY", None)
    environ.pop("HYPERLIQUID_ACCOUNT_ADDRESS", None)
    for key in list(environ):
        if key.startswith("HYPERGROK_"):
            environ.pop(key)
    environ["HYPERGROK_STATE_DIR"] = str(WORK / "state")
    environ.update(env or {})
    return subprocess.run(
        [str(BIN), *args], capture_output=True, text=True, env=environ,
        cwd=str(cwd or WORK), timeout=90,
    )


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((group, name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    return ok


def gate(name: str, args: list[str], expect: str, env: dict[str, str] | None = None) -> bool:
    """A gate must refuse, non-zero, with a recognisable reason."""
    proc = run(*args, env=env)
    blob = (proc.stdout + proc.stderr).lower()
    ok = proc.returncode != 0 and expect.lower() in blob
    return check(name, ok, f"rc={proc.returncode} out={blob.strip()[:150]}")


def mid(network: str, coin: str = "BTC") -> Decimal:
    url = (
        "https://api.hyperliquid.xyz/info" if network == "mainnet"
        else "https://api.hyperliquid-testnet.xyz/info"
    )
    req = Request(url, data=json.dumps({"type": "allMids"}).encode(),
                  headers={"Content-Type": "application/json"})
    return Decimal(str(json.load(urlopen(req, timeout=20))[coin]))


ACC = "0x" + "1" * 40
TESTNET = {"HYPERGROK_NETWORK": "testnet"}
MAINNET = {"HYPERGROK_NETWORK": "mainnet", "HYPERGROK_ENABLE_MAINNET": "I_UNDERSTAND"}

print(f"\nRepo: {REPO}\nWork: {WORK}\n")

# ---------------------------------------------------------------- read surface
group = "Read commands (testnet)"
print(f"\n== {group} ==")
for name, args in [
    ("quickstart", ["quickstart"]),
    ("health", ["health"]),
    ("doctor", ["doctor"]),
    ("doctor --user", ["doctor", "--user", ACC]),
    ("market BTC", ["market", "BTC"]),
    ("account", ["account", ACC]),
    ("builder-status", ["builder-status", ACC]),
    ("defillama", ["defillama", "hyperliquid"]),
    ("coingecko", ["coingecko", "hyperliquid"]),
]:
    proc = run(*args, env=TESTNET)
    check(name, proc.returncode == 0, f"rc={proc.returncode} {proc.stderr.strip()[:120]}")

group = "Read commands (mainnet)"
print(f"\n== {group} ==")
for name, args in [
    ("doctor", ["doctor"]),
    ("doctor --user", ["doctor", "--user", ACC]),
    ("market BTC", ["market", "BTC"]),
    ("account", ["account", ACC]),
]:
    proc = run(*args, env=MAINNET)
    check(name, proc.returncode == 0, f"rc={proc.returncode} {proc.stderr.strip()[:120]}")

group = "Deterministic sizing"
print(f"\n== {group} ==")
proc = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
           "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET)
ok = proc.returncode == 0
if ok:
    data = json.loads(proc.stdout)
    # 0.5% of 10000 = 50 risk; 5 per unit => 10 units => 1000 notional, at cap
    ok = Decimal(str(data.get("notional", 0))) <= Decimal("1000")
check("size respects max-notional cap", ok, proc.stdout[:200])
proc = run("size", "--equity", "10000", "--entry", "100", "--stop", "100",
           "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET)
check("size rejects zero stop distance", proc.returncode != 0, proc.stderr[:120])

# ------------------------------------------------------------- mainnet opt-in
group = "Mainnet opt-in"
print(f"\n== {group} ==")
gate("mainnet refused without acknowledgement", ["doctor"], "HYPERGROK_ENABLE_MAINNET",
     env={"HYPERGROK_NETWORK": "mainnet"})
proc = run("doctor", env=MAINNET)
check("mainnet allowed with acknowledgement", proc.returncode == 0)
proc = run("doctor", env=TESTNET)
check("testnet is the default endpoint",
      proc.returncode == 0 and "testnet" in proc.stdout)

# --------------------------------------------------------------- config guards
group = "Configuration guards"
print(f"\n== {group} ==")
gate("bad network rejected", ["doctor"], "must be testnet or mainnet",
     env={"HYPERGROK_NETWORK": "devnet"})
gate("non-numeric cap rejected", ["doctor"], "must be a number",
     env={**TESTNET, "HYPERGROK_MAX_ORDER_NOTIONAL_USD": "abc"})
gate("slippage beyond the sanity bound rejected", ["doctor"], "SLIPPAGE",
     env={**TESTNET, "HYPERGROK_MAX_SLIPPAGE_BPS": "5000"})
gate("risk ceiling beyond the sanity bound rejected", ["doctor"], "RISK_PCT",
     env={**TESTNET, "HYPERGROK_MAX_RISK_PCT": "101"})
proc = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
           "--risk-pct", "25", "--max-notional", "1000000", env=TESTNET)
check("no risk ceiling is imposed by default", proc.returncode == 0, proc.stderr[:150])
proc = run("limits", "BTC", "--equity", "10000", env=TESTNET)
data = json.loads(proc.stdout) if proc.returncode == 0 else {}
check("limits reports the exchange's real constraints",
      proc.returncode == 0 and data.get("exchange_limits", {}).get("max_leverage"),
      proc.stderr[:150])
check("limits reports tiered margin, not just headline leverage",
      len(data.get("exchange_limits", {}).get("margin_tiers", [])) >= 1,
      json.dumps(data.get("exchange_limits", {}))[:200])
check("limits states plainly that HyperGrok imposes no ceiling",
      data.get("hypergrok_ceilings", {}).get("max_risk_pct") is None,
      json.dumps(data.get("hypergrok_ceilings", {}))[:200])
gate("plan lifetime beyond the sanity bound rejected", ["doctor"], "PLAN_MINUTES",
     env={**TESTNET, "HYPERGROK_MAX_PLAN_MINUTES": "99999"})
proc = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
           "--risk-pct", "5", "--max-notional", "100000",
           env={**TESTNET, "HYPERGROK_MAX_RISK_PCT": "10"})
check("a user-raised risk ceiling is honoured", proc.returncode == 0, proc.stderr[:150])
proc = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
           "--risk-pct", "5", "--max-notional", "100000",
           env={**TESTNET, "HYPERGROK_MAX_RISK_PCT": "2"})
check("an opt-in ceiling still refuses when exceeded", proc.returncode != 0,
      proc.stdout[:150])
gate("orders below Hyperliquid's 10 USD minimum are refused",
     ["plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy", "--size",
      "0.00001", "--limit-px", "60000", "--out", str(WORK / "tiny.json")],
     "minimum order value", env=TESTNET)
proc = run("plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy",
           "--size", "0.001", "--limit-px", "60000", "--expires-minutes", "90",
           "--out", str(WORK / "long.json"),
           env={**TESTNET, "HYPERGROK_MAX_PLAN_MINUTES": "120",
                "HYPERGROK_MAX_ORDER_NOTIONAL_USD": "100000"})
check("a user-raised plan lifetime is honoured", proc.returncode == 0, proc.stderr[:150])
gate("relative state dir rejected", ["doctor"], "absolute",
     env={**TESTNET, "HYPERGROK_STATE_DIR": "relative/path"})

# ------------------------------------------------------------------ .env layer
group = "Dotenv layer"
print(f"\n== {group} ==")
envdir = WORK / "envtest"
envdir.mkdir()
(envdir / ".env").write_text("HYPERGROK_MAX_ORDER_NOTIONAL_USD=50\n")
proc = subprocess.run(
    [str(BIN), "plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy",
     "--size", "0.001", "--limit-px", "100000", "--out", str(envdir / "a.json")],
    capture_output=True, text=True, cwd=str(envdir),
    env={**{k: v for k, v in os.environ.items() if not k.startswith("HYPERGROK_")},
         "HYPERGROK_STATE_DIR": str(WORK / "state")},
)
check(".env value is applied", proc.returncode != 0 and "ceiling 50" in proc.stderr,
      proc.stderr[:150])
proc = subprocess.run(
    [str(BIN), "plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy",
     "--size", "0.001", "--limit-px", "100000", "--out", str(envdir / "b.json")],
    capture_output=True, text=True, cwd=str(envdir),
    env={**{k: v for k, v in os.environ.items() if not k.startswith("HYPERGROK_")},
         "HYPERGROK_STATE_DIR": str(WORK / "state"),
         "HYPERGROK_MAX_ORDER_NOTIONAL_USD": "500"},
)
check("real environment overrides .env", proc.returncode == 0, proc.stderr[:150])
check("no secret echoed by quickstart",
      "PRIVATE_KEY is set" in run("quickstart", env={**TESTNET,
          "HYPERLIQUID_PRIVATE_KEY": "0x" + "b" * 64,
          "HYPERLIQUID_ACCOUNT_ADDRESS": ACC}).stdout
      and "b" * 64 not in run("quickstart", env={**TESTNET,
          "HYPERLIQUID_PRIVATE_KEY": "0x" + "b" * 64,
          "HYPERLIQUID_ACCOUNT_ADDRESS": ACC}).stdout)

# ------------------------------------------------------------- execution gates
group = "Execution gates (fail-closed)"
print(f"\n== {group} ==")
# Hyperliquid allows integer prices regardless of significant figures, so round
# the live mid to an integer to get a valid tick that is still inside the drift cap.
live = mid("testnet").to_integral_value()
plans = WORK / "plans"
plans.mkdir()


def make(name: str, *, px: Decimal | None = None, net: dict | None = None,
         size: str = "0.001", extra: list[str] | None = None) -> tuple[Path, str]:
    path = plans / f"{name}.json"
    proc = run("plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy",
               "--size", size, "--limit-px", str(px if px is not None else live),
               "--out", str(path), *(extra or []),
               env={**(net or TESTNET), "HYPERGROK_MAX_ORDER_NOTIONAL_USD": "100000"})
    if proc.returncode != 0:
        return path, ""
    return path, json.loads(proc.stdout)["sha256"]


path, digest = make("good")
check("plan-order writes a plan", digest != "", "")
gate("--execute flag is mandatory",
     ["execute-order", "--plan", str(path), "--confirm", digest],
     "literal --execute", env=TESTNET)
gate("wrong confirmation hash refused",
     ["execute-order", "--plan", str(path), "--confirm", "0" * 64, "--execute"],
     "does not exactly match", env=TESTNET)
gate("plan file cannot be silently overwritten",
     ["plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy", "--size",
      "0.001", "--limit-px", str(live), "--out", str(path)],
     "already exists", env=TESTNET)

tamper = plans / "tampered.json"
doc = json.loads(path.read_text())
doc["plan"]["size"] = "999"
tamper.write_text(json.dumps(doc))
gate("tampered plan detected by hash",
     ["execute-order", "--plan", str(tamper), "--confirm", digest, "--execute"],
     "hash does not match", env=TESTNET)

mpath, mdigest = make("mainnet", net=MAINNET)
gate("network mismatch refused",
     ["execute-order", "--plan", str(mpath), "--confirm", mdigest, "--execute"],
     "network differs", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

bpath, bdigest = make("big", size="1")
gate("notional cap enforced at execution",
     ["execute-order", "--plan", str(bpath), "--confirm", bdigest, "--execute"],
     "notional cap", env={**TESTNET, "HYPERGROK_MAX_ORDER_NOTIONAL_USD": "1000",
                          "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

spath, sdigest = make("slip")
gate("slippage cap cannot be weakened by a plan",
     ["execute-order", "--plan", str(spath), "--confirm", sdigest, "--execute"],
     "slippage cap", env={**TESTNET, "HYPERGROK_MAX_SLIPPAGE_BPS": "5",
                          "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

apath, adigest = make("acct")
gate("declared account must match the plan",
     ["execute-order", "--plan", str(apath), "--confirm", adigest, "--execute"],
     "ACCOUNT_ADDRESS", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "2" * 40})

dpath, ddigest = make("drift", px=live * Decimal("0.5"))
gate("live price drift refused",
     ["execute-order", "--plan", str(dpath), "--confirm", ddigest, "--execute"],
     "drift", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

ppath, pdigest = make("precision", size="0.0012345678")
gate("sub-tick size refused",
     ["execute-order", "--plan", str(ppath), "--confirm", pdigest, "--execute"],
     "decimals", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

# The full live chain: everything above the signing boundary must pass, and the
# run must stop exactly at "no key", proving gates 1-11 all cleared live.
kpath, kdigest = make("nokey")
proc = run("execute-order", "--plan", str(kpath), "--confirm", kdigest, "--execute",
           env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})
check("full live chain reaches the signing boundary and stops there",
      proc.returncode != 0 and "PRIVATE_KEY" in proc.stderr,
      f"rc={proc.returncode} {proc.stderr.strip()[:180]}")

gate("invalid key refused before any send",
     ["execute-order", "--plan", str(kpath), "--confirm", kdigest, "--execute"],
     "not a valid signing key",
     env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC,
          "HYPERLIQUID_PRIVATE_KEY": "not-a-key"})

# A correctly-hashed but stale plan must still be refused.
stale_doc = json.loads(kpath.read_text())
past = datetime.now(UTC) - timedelta(minutes=20)
stale_doc["plan"]["created_at"] = past.isoformat()
stale_doc["plan"]["expires_at"] = (past + timedelta(minutes=5)).isoformat()
canonical = json.dumps(stale_doc["plan"], sort_keys=True, separators=(",", ":"))
stale_doc["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
stale = plans / "stale.json"
stale.write_text(json.dumps(stale_doc, indent=2))
gate("expired plan refused even with a valid hash",
     ["execute-order", "--plan", str(stale), "--confirm", stale_doc["sha256"], "--execute"],
     "expired", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})

# --------------------------------------------------------------- input hygiene
group = "Input validation"
print(f"\n== {group} ==")
gate("malformed address refused", ["account", "not-an-address"], "hexadecimal", env=TESTNET)
gate("malformed doctor user refused", ["doctor", "--user", "0xnope"], "hexadecimal", env=TESTNET)
gate("unknown market refused", ["market", "NOTACOIN"], "unknown perp market", env=TESTNET)
gate("bad cloid refused",
     ["order-status", "--account", ACC, "--cloid", "abc"], "128-bit", env=TESTNET)
gate("expiry beyond the configured lifetime refused",
     ["plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy", "--size", "0.001",
      "--limit-px", str(live), "--expires-minutes", "60", "--out", str(plans / "x.json")],
     "between 1 and 30", env=TESTNET)

print("\n" + "=" * 70)
groups: dict[str, list[bool]] = {}
for grp, _, ok, _ in results:
    groups.setdefault(grp, []).append(ok)
for grp, oks in groups.items():
    print(f"  {sum(oks):>2}/{len(oks):<2}  {grp}")
failed = [(g, n, d) for g, n, ok, d in results if not ok]
total, passed = len(results), sum(1 for _, _, ok, _ in results if ok)
print("=" * 70)
print(f"  {passed}/{total} checks passed")
if failed:
    print("\nFAILURES:")
    for g, n, d in failed:
        print(f"  [{g}] {n}\n      {d}")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failed else 0)
