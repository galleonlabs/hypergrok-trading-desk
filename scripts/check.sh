#!/usr/bin/env bash
# Lints the repository's instruction files. Stdlib Python only; no network.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import os, re, sys, glob

errors = []
def err(msg): errors.append(msg)

def frontmatter(path):
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---\n"):
        err(f"{path}: missing frontmatter"); return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        err(f"{path}: unterminated frontmatter"); return {}, text
    fm, body = text[4:end], text[end + 5:]
    data, key = {}, None
    for line in fm.splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            data[key] = [] if val == "" else val
        elif key and re.match(r"^\s+-\s+", line):
            if not isinstance(data.get(key), list): data[key] = []
            data[key].append(line.strip()[2:].strip())
        elif key and re.match(r"^\s+[A-Za-z_-]+:", line):
            pass  # nested metadata map; not validated
    return data, body

skills = sorted(d for d in glob.glob("skills/*") if os.path.isdir(d))
skill_names = set()
for d in skills:
    path = os.path.join(d, "SKILL.md")
    if not os.path.exists(path):
        err(f"{d}: missing SKILL.md"); continue
    fm, body = frontmatter(path)
    name = fm.get("name")
    if name != os.path.basename(d):
        err(f"{path}: name '{name}' does not match directory")
    skill_names.add(os.path.basename(d))
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc:
        err(f"{path}: missing description")
    elif len(desc) > 1024:
        err(f"{path}: description is {len(desc)} chars (limit 1024)")
    lines = body.count("\n")
    if lines > 320:
        err(f"{path}: body is {lines} lines (budget 320)")
    if fm.get("license") != "MIT":
        err(f"{path}: license should be MIT")

agents = sorted(glob.glob("agents/*.md"))
agent_names = set()
for path in agents:
    fm, body = frontmatter(path)
    stem = os.path.basename(path)[:-3]
    if fm.get("name") != stem:
        err(f"{path}: name '{fm.get('name')}' does not match filename")
    agent_names.add(stem)
    for key in ("title", "description", "seat", "writes_to_exchange"):
        if key not in fm: err(f"{path}: missing '{key}'")
    for s in fm.get("skills", []) or []:
        if s not in skill_names: err(f"{path}: references unknown skill '{s}'")
    if "## Bot profile" not in body or "## System prompt" not in body:
        err(f"{path}: needs '## Bot profile' and '## System prompt' sections")
    if fm.get("seat") not in ("floor", "off-floor"):
        err(f"{path}: seat must be floor or off-floor")

writers = [p for p in agents if frontmatter(p)[0].get("writes_to_exchange") == "true"]
if len(writers) != 1:
    err(f"exactly one agent may write to the exchange; found {len(writers)}: {writers}")

# Every skill and agent is listed in SETUP.md and skills/README.md
setup = open("SETUP.md", encoding="utf-8").read()
index = open("skills/README.md", encoding="utf-8").read()
for s in sorted(skill_names):
    if f"`{s}`" not in setup: err(f"SETUP.md does not list skill {s}")
    if f"[{s}]({s}/SKILL.md)" not in index: err(f"skills/README.md does not link skill {s}")
for a in sorted(agent_names):
    if f"agents/{a}.md" not in setup: err(f"SETUP.md does not list agents/{a}.md")

# Internal markdown links resolve
md_files = [p for p in glob.glob("**/*.md", recursive=True) if not p.startswith("node_modules")]
link_re = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
for path in md_files:
    text = open(path, encoding="utf-8").read()
    for target in link_re.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")): continue
        target = target.split("#")[0]
        if not target: continue
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
        if not os.path.exists(resolved):
            err(f"{path}: broken link -> {target}")

# No references to the retired CLI
cli_re = re.compile(r"\bhypergrok (doctor|market|limits|size|plan-order|execute-order|quickstart|account|defillama|coingecko|order-status)\b|pip install -e|src/hypergrok|hypergrok\.cli")
for path in md_files + glob.glob("rules/*.mdc") + glob.glob("**/*.json", recursive=True):
    if path.startswith(("CHANGELOG", "docs/PROVENANCE")): continue
    text = open(path, encoding="utf-8").read()
    if cli_re.search(text):
        err(f"{path}: references the retired hypergrok CLI")

# No emoji in instruction files
emoji_re = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
for path in md_files:
    if emoji_re.search(open(path, encoding="utf-8").read()):
        err(f"{path}: contains emoji")

if errors:
    print("\n".join(errors)); print(f"{len(errors)} problem(s)"); sys.exit(1)
print(f"ok: {len(skills)} skills, {len(agents)} agents, {len(md_files)} markdown files checked")
PY
