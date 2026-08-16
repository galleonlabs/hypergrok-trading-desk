import argparse
import json
import re
import tomllib
from pathlib import Path

from hypergrok import __version__
from hypergrok.cli import parser

ROOT = Path(__file__).parents[1]


def test_manifests_agree() -> None:
    root = json.loads((ROOT / "plugin.json").read_text())
    grok = json.loads((ROOT / ".grok-plugin/plugin.json").read_text())
    cursor = json.loads((ROOT / ".cursor-plugin/plugin.json").read_text())
    for field in ("name", "version", "repository", "license"):
        assert root[field] == grok[field]
    for field in ("name", "version"):
        assert root[field] == cursor[field]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert root["version"] == project["version"] == __version__
    assert cursor["author"]["name"] == "Galleon Labs"
    assert cursor["skills"] == "./skills/"
    assert cursor["agents"] == "./agents/"
    assert cursor["rules"] == "./rules/"
    assert set(grok) <= {
        "name", "version", "description", "author", "homepage", "repository", "license", "keywords"
    }
    assert "grok-bot" not in grok["keywords"]


def test_every_skill_has_valid_frontmatter() -> None:
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skills) == 11
    for path in skills:
        text = path.read_text()
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        fields = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                  for line in frontmatter.splitlines() if ":" in line and not line.startswith(" ")}
        assert fields["name"] == path.parent.name
        assert fields["description"].endswith(".")
        assert len(fields["description"]) <= 60
        assert fields["version"] == "1.0.0"
        assert "## Procedure" in text and "## Verification" in text


def test_every_agent_has_valid_frontmatter() -> None:
    agents = sorted((ROOT / "agents").glob("*.md"))
    assert len(agents) == 7
    for path in agents:
        text = path.read_text()
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        fields = dict(line.split(": ", 1) for line in frontmatter.splitlines() if ": " in line)
        assert fields["name"] == path.stem
        assert fields["description"].endswith(".")
        assert "## Boundaries" in text and "## Handoff" in text


def test_team_rule_and_bootstrap_are_shipped() -> None:
    rule = (ROOT / "rules/hypergrok-team.mdc").read_text()
    bootstrap = (ROOT / "BOOTSTRAP.md").read_text()
    assert "alwaysApply: true" in rule
    roles = (
        "desk lead",
        "market analyst",
        "onchain analyst",
        "risk officer",
        "execution trader",
        "portfolio manager",
        "trade reviewer",
    )
    for role in roles:
        assert role in rule.lower()
        assert role in bootstrap.lower()
    readme = (ROOT / "README.md").read_text()
    assert "Settings -> Plugins -> Yours" not in readme
    assert "Open Grok Bot and paste this:" in readme
    assert "blob/main/BOOTSTRAP.md" in readme
    assert "single entry point" in readme
    assert (ROOT / "docs/GROK_BOT.md").is_file()


def test_public_markdown_omits_internal_send_attribution() -> None:
    for path in ROOT.rglob("*.md"):
        if ".git" in path.parts:
            continue
        text = path.read_text().lower()
        assert "builder" not in text, path.relative_to(ROOT)
        assert "referral" not in text, path.relative_to(ROOT)


def test_skill_command_references_resolve() -> None:
    root_parser = parser()
    subparsers = next(
        action for action in root_parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    commands = set((subparsers.choices or {}).keys())
    referenced: set[str] = set()
    for path in (ROOT / "skills").glob("*/SKILL.md"):
        referenced.update(re.findall(r"hypergrok ([a-z][a-z-]+)", path.read_text()))
    assert referenced
    assert referenced <= commands


def test_live_smoke_is_read_only() -> None:
    text = (ROOT / "scripts/live_smoke.py").read_text()
    assert "hyperliquid.exchange" not in text
    assert "HYPERLIQUID_PRIVATE_KEY" not in text
    assert ".order(" not in text
    assert set(("testnet", "mainnet")) <= set(re.findall(r'"(testnet|mainnet)"', text))
