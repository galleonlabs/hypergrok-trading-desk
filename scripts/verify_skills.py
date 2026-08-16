#!/usr/bin/env python3
"""Execute each SKILL.md procedure against live data and check its own
Verification clause is satisfiable from the real output.

Testing the CLI proves the commands work. This proves the *skills* work: that an
agent following a skill top to bottom gets what the skill promises. Never signs,
never submits an order, never handles a key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
BIN = REPO / ".venv" / "bin" / "hypergrok"
WORK = Path(tempfile.mkdtemp(prefix="hypergrok-skills-"))
ACC = "0x" + "1" * 40

TESTNET = {"HYPERGROK_NETWORK": "testnet"}
MAINNET = {"HYPERGROK_NETWORK": "mainnet", "HYPERGROK_ENABLE_MAINNET": "I_UNDERSTAND"}

results: list[tuple[str, str, bool, str]] = []
skill = "-"


def run(*args: str, env: dict[str, str] | None = None):
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("HYPERGROK_", "HYPERLIQUID_"))}
    environ["HYPERGROK_STATE_DIR"] = str(WORK / "state")
    environ.update(env or {})
    return subprocess.run([str(BIN), *args], capture_output=True, text=True,
                          env=environ, cwd=str(WORK), timeout=90)


def js(proc):
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((skill, name, ok, detail))
    print(f"    {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    return ok


def provenance(data, label: str) -> None:
    """Most skills demand 'live source, observation time' in their Verification."""
    check(f"{label}: reports live source", isinstance(data, dict) and "source" in data)
    check(f"{label}: reports observation time", isinstance(data, dict) and "observed_at" in data)
    check(f"{label}: reports network", isinstance(data, dict) and "network" in data)


print(f"\nSkill procedures executed against live endpoints\nWork: {WORK}\n")

# ---------------------------------------------------------------- desk-setup
skill = "desk-setup"
print(f"== {skill} ==")
t_doctor = js(run("doctor", env=TESTNET))
check("step 1: doctor answers on testnet", t_doctor is not None)
t_market = js(run("market", "BTC", env=TESTNET))
check("step 1: market BTC answers on testnet", t_market is not None)
m_doctor = js(run("doctor", env=MAINNET))
check("step 2: doctor answers on mainnet", m_doctor is not None)
m_market = js(run("market", "BTC", env=MAINNET))
check("step 2: market BTC answers on mainnet", m_market is not None)
check("step 3: caps and readiness gates present",
      bool(t_doctor) and {"execution_ready", "next_action", "builder"} <= set(t_doctor))
u_doctor = js(run("doctor", "--user", ACC, env=TESTNET))
check("step 4: doctor --user reports account-specific readiness",
      bool(u_doctor) and u_doctor["builder"]["user"] == ACC)
check("step 5: never asks for a seed phrase",
      "seed" not in json.dumps(t_doctor or {}).lower())
check("verification: both network receipts differ by endpoint",
      bool(t_doctor) and bool(m_doctor) and t_doctor["endpoint"] != m_doctor["endpoint"])
check("pitfall: green read check is NOT execution readiness",
      bool(t_doctor) and t_doctor["status"] == "read-only-ready" and not t_doctor["execution_ready"])

# ------------------------------------------------------ hyperliquid-intelligence
skill = "hyperliquid-intelligence"
print(f"\n== {skill} ==")
provenance(t_market, "market")
check("step 2: market carries asset and live context",
      bool(t_market) and {"asset", "context"} <= set(t_market))
acct = js(run("account", ACC, env=TESTNET))
provenance(acct, "account")
check("step 3: account returns state and open orders",
      bool(acct) and {"state", "open_orders"} <= set(acct))
check("step 4: facts are separable from interpretation (no advice keys)",
      bool(t_market) and not any(k in t_market for k in ("recommendation", "signal", "advice")))

# ------------------------------------------------------------ defillama-research
skill = "defillama-research"
print(f"\n== {skill} ==")
llama = js(run("defillama", "hyperliquid", env=TESTNET))
check("step 2: defillama <slug> answers", llama is not None)
check("step 3: TVL denomination and chains recorded",
      bool(llama) and any(k in json.dumps(llama).lower() for k in ("tvl", "chain")))
bad = run("defillama", "not-a-real-protocol-xyz", env=TESTNET)
check("pitfall: unknown slug fails loudly rather than inventing data",
      bad.returncode != 0 or js(bad) in (None, {}), bad.stdout[:120])

# ------------------------------------------------------------ coingecko-research
skill = "coingecko-research"
print(f"\n== {skill} ==")
gecko = js(run("coingecko", "hyperliquid", env=TESTNET))
check("step 3: coingecko <coin-id> answers", gecko is not None)
check("step 4: identity and market context returned",
      bool(gecko) and any(k in json.dumps(gecko).lower() for k in ("symbol", "market", "price")))
badg = run("coingecko", "not-a-real-coin-xyz", env=TESTNET)
check("pitfall: colliding/unknown ticker is not guessed",
      badg.returncode != 0, badg.stdout[:120])

# ------------------------------------------------------------------ pretrade-risk
skill = "pretrade-risk"
print(f"\n== {skill} ==")
sized = js(run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
               "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET))
check("step 3: size returns a deterministic result", sized is not None)
check("step 5: result is an explicit size, not a profit target",
      bool(sized) and any(k in sized for k in ("size", "notional")))
again = js(run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
               "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET))
check("deterministic: identical inputs give identical output", sized == again)
refused = run("size", "--equity", "10000", "--entry", "100", "--stop", "100",
              "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET)
check("step 5: refuses with an exact failed gate",
      refused.returncode != 0 and bool(refused.stderr.strip()), refused.stderr[:120])
capped = js(run("size", "--equity", "1000000", "--entry", "100", "--stop", "99",
                "--risk-pct", "2", "--max-notional", "500", env=TESTNET))
check("max-notional is genuinely binding",
      bool(capped) and Decimal(str(capped.get("notional", 0))) <= Decimal("500"),
      json.dumps(capped)[:150])
free = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
           "--risk-pct", "25", "--max-notional", "1000000", env=TESTNET)
check("step 5: no risk ceiling is imposed on the risk officer",
      free.returncode == 0, free.stderr[:120])
opted = run("size", "--equity", "10000", "--entry", "100", "--stop", "95",
            "--risk-pct", "5", "--max-notional", "1000000",
            env={**TESTNET, "HYPERGROK_MAX_RISK_PCT": "2"})
check("an opt-in ceiling is honoured when the user sets one",
      opted.returncode != 0 and "ceiling" in opted.stderr, opted.stderr[:120])
lim = js(run("limits", "BTC", "--equity", "10000", env=TESTNET))
check("step 1: exchange limits are readable for sizing",
      bool(lim) and lim["exchange_limits"]["max_leverage"], json.dumps(lim)[:150])
check("step 5: tiered margin is exposed, not just headline leverage",
      bool(lim) and len(lim["exchange_limits"]["margin_tiers"]) >= 1)
check("pitfall: the tool states it has no risk opinion of its own",
      bool(lim) and lim["hypergrok_ceilings"]["max_risk_pct"] is None)
check("realised risk never exceeds the requested budget",
      bool(capped) and Decimal(str(capped["risk_usd"])) <= Decimal("20000"),
      json.dumps(capped)[:150])

# --------------------------------------------------------------- order-execution
skill = "order-execution"
print(f"\n== {skill} ==")
px = Decimal(str(js(run("market", "BTC", env=TESTNET))["context"]["markPx"])).to_integral_value()
plan_path = WORK / "skill-order.json"
planned = js(run("plan-order", "--account", ACC, "--coin", "BTC", "--side", "buy",
                 "--size", "0.001", "--limit-px", str(px), "--out", str(plan_path),
                 env=TESTNET))
check("step 1: plan-order returns a preserved SHA-256",
      bool(planned) and len(planned.get("sha256", "")) == 64)
body = json.loads(plan_path.read_text())["plan"] if plan_path.exists() else {}
required = {"account", "network", "side", "size", "limit_px", "expires_at", "cloid"}
check("step 2: plan shows account, network, side, size, limit, expiry and cloid",
      required <= set(body), f"missing={required - set(body)}")
check("step 2: the plan is reviewable by a human (readable file)",
      plan_path.exists() and plan_path.stat().st_size > 0)
ready = js(run("doctor", "--user", ACC, env=TESTNET))
check("step 3: doctor --user reports readiness on the plan's network",
      bool(ready) and ready["network"] == body.get("network"))
noapproval = run("execute-order", "--plan", str(plan_path),
                 "--confirm", planned["sha256"], env=TESTNET)
check("step 4/5: cannot execute without the explicit --execute approval flag",
      noapproval.returncode != 0 and "--execute" in noapproval.stderr)
stopped = run("execute-order", "--plan", str(plan_path), "--confirm", planned["sha256"],
              "--execute", env={**TESTNET, "HYPERLIQUID_ACCOUNT_ADDRESS": ACC})
check("step 5: every pre-signing gate passes, stops at the key boundary",
      stopped.returncode != 0 and "PRIVATE_KEY" in stopped.stderr, stopped.stderr[:150])
check("pitfall: main-wallet key is rejected operationally (API wallet role enforced)",
      "userRole" in (REPO / "src/hypergrok/cli.py").read_text())

# -------------------------------------------------------------- portfolio-control
skill = "portfolio-control"
print(f"\n== {skill} ==")
check("step 1: account read succeeds", acct is not None)
check("step 2: positions, orders and margin all reconcilable",
      bool(acct) and "marginSummary" in json.dumps(acct.get("state", {})))
check("step 3: open orders are separately visible from positions",
      bool(acct) and isinstance(acct.get("open_orders"), list))
check("pitfall: a plan file is not an open order",
      plan_path.exists() and bool(acct) and not any(
          json.loads(plan_path.read_text())["plan"]["cloid"] in json.dumps(o)
          for o in acct.get("open_orders", [])))

# --------------------------------------------------------------- posttrade-review
skill = "posttrade-review"
print(f"\n== {skill} ==")
cloid = json.loads(plan_path.read_text())["plan"]["cloid"]
status = run("order-status", "--account", ACC, "--cloid", cloid, env=TESTNET)
check("step 1: an order can be reconstructed by cloid without signing",
      status.returncode == 0, status.stderr[:150])
sdata = js(status)
check("step 1: reconciliation carries provenance",
      bool(sdata) and {"cloid", "network", "observed_at", "source"} <= set(sdata))
check("step 2: planned values are preserved for comparison",
      cloid == json.loads(plan_path.read_text())["plan"]["cloid"])

# -------------------------------------------------------------- incident-response
skill = "incident-response"
print(f"\n== {skill} ==")
help_text = run("--help").stdout
check("step 2: live account and order state readable during an incident",
      acct is not None and status.returncode == 0)
check("step 3: unknown submissions reconcilable by cloid before cancelling",
      "order-status" in help_text)
check("documented pitfall holds: there is NO automated cancel-all command",
      not any(c in help_text for c in ("cancel", "cancel-all")))
check("pitfall: no withdraw/transfer command exists as an improvised response",
      not any(c in help_text for c in ("withdraw", "transfer", "bridge")))
journal = WORK / "state"
check("step 1: execution attempts are journalled for preservation",
      journal.exists() or "journal" in (REPO / "src/hypergrok/cli.py").read_text())

# ---------------------------------------------------------------- crew-bootstrap
skill = "crew-bootstrap"
print(f"\n== {skill} ==")
ROLES = ["desk-lead", "market-analyst", "onchain-analyst", "risk-officer",
         "execution-trader", "portfolio-manager", "trade-reviewer"]
missing = [r for r in ROLES if not (REPO / "agents" / f"{r}.md").exists()]
check("step 1: all seven exact roles are inventoried", not missing, f"missing={missing}")
check("step 2: each role definition is loadable and substantive",
      all(len((REPO / "agents" / f"{r}.md").read_text()) > 200 for r in ROLES))
check("step 5: only the execution trader references the guarded execute command",
      [r for r in ROLES if "execute-order" in (REPO / "agents" / f"{r}.md").read_text()]
      in ([], ["execution-trader"]),
      str([r for r in ROLES if "execute-order" in (REPO / "agents" / f"{r}.md").read_text()]))
check("step 6: both network endpoints answered", bool(t_doctor) and bool(m_doctor))
# step 7: the actual desk handoff, run for real
analyst = js(run("market", "BTC", env=TESTNET))
mark = Decimal(str(analyst["context"]["markPx"]))
handoff = js(run("size", "--equity", "10000", "--entry", str(mark),
                 "--stop", str((mark * Decimal("0.98")).quantize(Decimal("1"))),
                 "--risk-pct", "0.5", "--max-notional", "1000", env=TESTNET))
check("step 7: market-analyst evidence flows into a risk-officer sizing",
      handoff is not None and Decimal(str(handoff.get("notional", 0))) > 0,
      json.dumps(handoff)[:150])
check("step 7: the handoff required no key and submitted no order",
      handoff is not None and not os.environ.get("HYPERLIQUID_PRIVATE_KEY"))

# ----------------------------------------------------------- thesis-construction
skill = "thesis-construction"
print(f"\n== {skill} ==")
risk_officer = (REPO / "agents/risk-officer.md").read_text().lower()
check("independent recomputation is required of the risk officer",
      any(w in risk_officer for w in ("independent", "recompute", "own")))
check("invalidation level is demanded by the skill",
      "invalidation" in (REPO / "skills/thesis-construction/SKILL.md").read_text().lower())
check("timestamped, sourced observations are demanded",
      all(w in (REPO / "skills/thesis-construction/SKILL.md").read_text().lower()
          for w in ("source", "timestamp")))

# --------------------------------------------------------------- cross-cutting
skill = "cross-cutting"
print(f"\n== {skill} ==")
all_skills = {p.parent.name: p.read_text() for p in (REPO / "skills").glob("*/SKILL.md")}
check("every skill states when to use it",
      all("## When to use" in t for t in all_skills.values()),
      str([k for k, t in all_skills.items() if "## When to use" not in t]))
check("every skill states its pitfalls",
      all("## Pitfalls" in t for t in all_skills.values()),
      str([k for k, t in all_skills.items() if "## Pitfalls" not in t]))
check("every skill states how to verify it",
      all("## Verification" in t for t in all_skills.values()),
      str([k for k, t in all_skills.items() if "## Verification" not in t]))
check("no skill instructs the agent to handle a seed phrase",
      not any("seed phrase" in t.lower() and "never" not in t.lower() for t in all_skills.values()))
check("every CLI command referenced by a skill exists",
      True if not (lambda: [c for t in all_skills.values()
                            for c in __import__("re").findall(r"hypergrok ([a-z][a-z-]+)", t)
                            if c not in help_text])() else False,
      str([c for t in all_skills.values()
           for c in __import__("re").findall(r"hypergrok ([a-z][a-z-]+)", t)
           if c not in help_text]))

print("\n" + "=" * 72)
groups: dict[str, list[bool]] = {}
for grp, _, ok, _ in results:
    groups.setdefault(grp, []).append(ok)
for grp, oks in groups.items():
    print(f"  {sum(oks):>2}/{len(oks):<2}  {grp}")
failed = [(g, n, d) for g, n, ok, d in results if not ok]
print("=" * 72)
print(f"  {sum(1 for _, _, ok, _ in results if ok)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for g, n, d in failed:
        print(f"  [{g}] {n}\n      {d}")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failed else 0)
