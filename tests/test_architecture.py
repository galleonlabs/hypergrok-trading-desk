from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "trading_harness"
FORBIDDEN_IMPORT_ROOTS = {
    "anthropic",
    "autogen",
    "codex",
    "crewai",
    "grok",
    "langchain",
    "openai",
    "xai",
}


class AgentRuntimeBoundaryTests(unittest.TestCase):
    def test_core_has_no_model_or_agent_runtime_imports(self) -> None:
        violations: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".", 1)[0]
                    if root in FORBIDDEN_IMPORT_ROOTS:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
        self.assertEqual([], violations)

    def test_legacy_model_plugin_entrypoints_are_absent(self) -> None:
        forbidden = (
            ROOT / "plugin.json",
            ROOT / ".grok-plugin",
            ROOT / ".claude-plugin",
            ROOT / ".cursor-plugin",
            ROOT / "agents",
            ROOT / "rules",
            ROOT / "skills",
            ROOT / "SETUP.md",
        )
        present = [str(path.relative_to(ROOT)) for path in forbidden if path.exists()]
        self.assertEqual([], present)


if __name__ == "__main__":
    unittest.main()
