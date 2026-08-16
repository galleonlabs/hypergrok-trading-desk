#!/usr/bin/env python3
"""Packaging and agent-layer verification: the surfaces a Grok Bot user touches."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
results: list[tuple[str, str, bool, str]] = []
group = "general"

AGENTS = ["desk-lead", "market-analyst", "onchain-analyst", "portfolio-manager",
          "risk-officer", "execution-trader", "trade-reviewer"]
SKILLS = ["coingecko-research", "crew-bootstrap", "defillama-research", "desk-setup",
          "hyperliquid-intelligence", "incident-response", "order-execution",
          "portfolio-control", "posttrade-review", "pretrade-risk", "thesis-construction"]


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((group, name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    return ok


# ------------------------------------------------------------------- packaging
group = "Packaging"
print(f"\n== {group} ==")
work = Path(tempfile.mkdtemp(prefix="hypergrok-pkg-"))
build = subprocess.run([str(REPO / ".venv/bin/python"), "-m", "build", "--wheel",
                        "--outdir", str(work)], capture_output=True, text=True, cwd=str(REPO))
check("wheel builds", build.returncode == 0, build.stderr[-200:])
wheels = list(work.glob("*.whl"))
check("wheel artefact produced", bool(wheels), str(list(work.iterdir())))

if wheels:
    venv = work / "v"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True)
    inst = subprocess.run([str(venv / "bin/pip"), "install", "-q", str(wheels[0])],
                          capture_output=True, text=True)
    check("wheel installs cleanly", inst.returncode == 0, inst.stderr[-250:])
    exe = venv / "bin" / "hypergrok"
    check("console script is created", exe.exists())
    if exe.exists():
        proc = subprocess.run([str(exe), "size", "--equity", "1000", "--entry", "100",
                               "--stop", "95", "--risk-pct", "0.5", "--max-notional", "200"],
                              capture_output=True, text=True)
        check("installed wheel runs offline command", proc.returncode == 0, proc.stderr[:200])
        proc = subprocess.run([str(exe), "--help"], capture_output=True, text=True)
        check("quickstart advertised in --help",
              "quickstart" in proc.stdout, proc.stdout[:200])

pyproject = (REPO / "pyproject.toml").read_text()
check("python floor is declared 3.11+", 'requires-python = ">=3.11"' in pyproject)
check("uv lockfile present", (REPO / "uv.lock").exists())
if shutil.which("uv"):
    # Sync into a copy so the repository tree is never mutated by verification.
    copy = work / "uvcopy"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__"))
    proc = subprocess.run(["uv", "sync", "--frozen"], capture_output=True, text=True,
                          cwd=str(copy), timeout=300)
    check("uv sync --frozen succeeds", proc.returncode == 0, proc.stderr[-250:])
    check("uv path produces the console script", (copy / ".venv/bin/hypergrok").exists())
else:
    check("uv path verified", False, "uv not installed; documented Grok Build path unverified here")

# ----------------------------------------------------------------- agent layer
group = "Agent and skill layer"
print(f"\n== {group} ==")
for agent in AGENTS:
    path = REPO / "agents" / f"{agent}.md"
    body = path.read_text() if path.exists() else ""
    check(f"agent present and non-trivial: {agent}", path.exists() and len(body) > 200,
          f"{len(body)} bytes")

for skill in SKILLS:
    path = REPO / "skills" / skill / "SKILL.md"
    if not path.exists():
        check(f"skill present: {skill}", False, "missing")
        continue
    text = path.read_text()
    fm = text.startswith("---") and "\n---" in text[3:]
    name_ok = re.search(r"^name:\s*\S+", text, re.M) is not None
    desc_ok = re.search(r"^description:\s*\S+", text, re.M) is not None
    check(f"skill frontmatter valid: {skill}", fm and name_ok and desc_ok,
          f"frontmatter={fm} name={name_ok} desc={desc_ok}")

# every hypergrok command referenced anywhere in docs/skills must exist
help_out = subprocess.run([str(REPO / ".venv/bin/hypergrok"), "--help"],
                          capture_output=True, text=True).stdout
commands = set(re.findall(r"^\s{4}(\w[\w-]+)", help_out, re.M))
choices = re.findall(r"\{([a-z,\-]+)\}", help_out)
if choices:
    commands |= set(choices[0].split(","))
referenced: dict[str, set[str]] = {}
for path in list(REPO.rglob("*.md")):
    if ".git" in path.parts or "/.venv/" in str(path) or "launch/" in str(path):
        continue
    for cmd in re.findall(r"hypergrok ([a-z][a-z-]+)", path.read_text()):
        referenced.setdefault(cmd, set()).add(str(path.relative_to(REPO)))
unknown = {c: v for c, v in referenced.items() if c not in commands}
check("every documented command exists", not unknown, f"unknown={unknown}")
check("commands were actually discovered", bool(commands) and bool(referenced),
      f"commands={sorted(commands)}")

# ------------------------------------------------------------ plugin manifests
group = "Plugin manifests"
print(f"\n== {group} ==")
for manifest in ["plugin.json", ".grok-plugin/plugin.json", ".cursor-plugin/plugin.json"]:
    path = REPO / manifest
    if not path.exists():
        check(f"{manifest} present", False, "missing")
        continue
    try:
        data = json.loads(path.read_text())
        check(f"{manifest} is valid JSON", True)
    except Exception as exc:
        check(f"{manifest} is valid JSON", False, str(exc))
        continue
    refs = [v for v in json.dumps(data).split('"') if v.endswith(".md") or v in ("agents", "skills", "rules")]
    missing = [r for r in refs if not (REPO / r).exists()]
    check(f"{manifest} paths resolve", not missing, f"missing={missing}")

rules = REPO / "rules" / "hypergrok-team.mdc"
check("cursor team rule present", rules.exists() and len(rules.read_text()) > 100)

# ------------------------------------------------------------- doc link health
group = "Documentation links"
print(f"\n== {group} ==")
for doc in ["README.md", "BOOTSTRAP.md", "docs/GROK_BOT.md"]:
    path = REPO / doc
    if not path.exists():
        check(f"{doc} present", False, "missing")
        continue
    broken = []
    for target in re.findall(r"\]\((?!https?://|#)([^)]+)\)", path.read_text()):
        clean = target.split("#")[0]
        if not clean:
            continue
        resolved = (path.parent / clean).resolve()
        if not resolved.exists():
            broken.append(target)
    check(f"{doc} internal links resolve", not broken, f"broken={broken}")

bootstrap = (REPO / "BOOTSTRAP.md").read_text()
check("BOOTSTRAP names all seven roles",
      all(r.replace("-", " ") in bootstrap.lower() for r in AGENTS),
      [r for r in AGENTS if r.replace("-", " ") not in bootstrap.lower()])
readme = (REPO / "README.md").read_text()
check("README points beginners at quickstart first",
      readme.index("hypergrok quickstart") < readme.index("hypergrok doctor"))
check("README tells users never to use a seed phrase",
      "seed phrase" in readme.lower())

shutil.rmtree(work, ignore_errors=True)

print("\n" + "=" * 70)
groups: dict[str, list[bool]] = {}
for grp, _, ok, _ in results:
    groups.setdefault(grp, []).append(ok)
for grp, oks in groups.items():
    print(f"  {sum(oks):>2}/{len(oks):<2}  {grp}")
failed = [(g, n, d) for g, n, ok, d in results if not ok]
print("=" * 70)
print(f"  {sum(1 for _, _, ok, _ in results if ok)}/{len(results)} checks passed")
if failed:
    print("\nFAILURES:")
    for g, n, d in failed:
        print(f"  [{g}] {n}\n      {d}")
sys.exit(1 if failed else 0)
