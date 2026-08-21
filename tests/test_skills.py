from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".agents" / "skills"


class RepoSkillContractTests(unittest.TestCase):
    def test_repo_skills_have_discoverable_frontmatter_and_resources(self) -> None:
        skill_dirs = sorted(path for path in SKILLS.iterdir() if path.is_dir())
        self.assertEqual(
            ["scan-signals", "validate-thesis"],
            [path.name for path in skill_dirs],
        )

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
            self.assertNotIn("TODO", metadata)


if __name__ == "__main__":
    unittest.main()
