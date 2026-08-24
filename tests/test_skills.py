from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".agents" / "skills"
PLUGIN_SKILLS = ROOT / "plugins" / "trading-desk" / "skills"
EXPECTED_SKILLS = [
    "assess-asset",
    "brief-market",
    "operate-trading-desk",
    "scan-signals",
    "test-strategy",
    "validate-thesis",
]


class RepoSkillContractTests(unittest.TestCase):
    def test_repo_skills_have_discoverable_frontmatter_and_resources(self) -> None:
        skill_dirs = sorted(path for path in PLUGIN_SKILLS.iterdir() if path.is_dir())
        self.assertEqual(EXPECTED_SKILLS, [path.name for path in skill_dirs])

        for skill_dir in skill_dirs:
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), skill_dir)
            _, frontmatter, body = text.split("---", 2)
            name = re.search(r"(?m)^name:\s*([^\n]+)$", frontmatter)
            description = re.search(r"(?m)^description:\s*([^\n]+)$", frontmatter)
            self.assertIsNotNone(name, skill_dir)
            self.assertIsNotNone(description, skill_dir)
            self.assertEqual(skill_dir.name, name.group(1).strip())
            self.assertGreater(len(description.group(1).strip()), 30)
            self.assertNotIn("TODO", text)

            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", body):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (skill_dir / target).resolve()
                self.assertTrue(resolved.exists(), f"{skill_dir.name}: {target}")

            metadata = (skill_dir / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"$" + skill_dir.name, metadata)
            self.assertIn("allow_implicit_invocation: true", metadata)
            self.assertNotIn("TODO", metadata)

    def test_repo_agent_skills_are_exact_plugin_mirror(self) -> None:
        mirrored = sorted(path.name for path in SKILLS.iterdir() if path.is_dir())
        self.assertEqual(EXPECTED_SKILLS, mirrored)
        for name in EXPECTED_SKILLS:
            plugin_files = {
                path.relative_to(PLUGIN_SKILLS / name): path.read_bytes()
                for path in (PLUGIN_SKILLS / name).rglob("*")
                if path.is_file()
            }
            agent_files = {
                path.relative_to(SKILLS / name): path.read_bytes()
                for path in (SKILLS / name).rglob("*")
                if path.is_file()
            }
            self.assertEqual(plugin_files, agent_files, name)

    def test_agent_skills_keep_direct_write_regression_tokens_absent(self) -> None:
        forbidden = (
            "HYPERLIQUID_PRIVATE_KEY",
            "exchange.order(",
            "exchange.bulk_orders(",
            "load_key()",
            "secret_key",
        )
        for path in PLUGIN_SKILLS.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, path)


if __name__ == "__main__":
    unittest.main()
