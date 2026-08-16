import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_manifests_agree() -> None:
    root = json.loads((ROOT / "plugin.json").read_text())
    grok = json.loads((ROOT / ".grok-plugin/plugin.json").read_text())
    for field in ("name", "version", "repository", "license"):
        assert root[field] == grok[field]


def test_every_skill_has_valid_frontmatter() -> None:
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skills) == 10
    for path in skills:
        text = path.read_text()
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        fields = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                  for line in frontmatter.splitlines() if ":" in line and not line.startswith(" ")}
        assert fields["name"] == path.parent.name
        assert fields["description"].endswith(".")
        assert len(fields["description"]) <= 60
        assert "## Procedure" in text and "## Verification" in text
