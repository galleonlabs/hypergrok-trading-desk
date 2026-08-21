from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.sync_plugin_runtime import synchronize


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "trading_harness"
RUNTIME = ROOT / "plugins" / "trading-desk" / "runtime" / "trading_harness"


class PluginRuntimeMirrorTests(unittest.TestCase):
    def test_runtime_is_an_exact_flat_python_mirror(self) -> None:
        source = {path.name: path.read_bytes() for path in SOURCE.glob("*.py")}
        runtime = {path.name: path.read_bytes() for path in RUNTIME.glob("*.py")}

        self.assertIn("__init__.py", source)
        self.assertEqual(source, runtime)

    def test_checked_in_sync_check_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/sync_plugin_runtime.py", "--check"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("current", result.stdout)

    def test_sync_check_is_read_only_and_sync_removes_only_stale_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "__init__.py").write_text("# package\n", encoding="utf-8")
            (source / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            (target / "__init__.py").write_text("# stale\n", encoding="utf-8")
            (target / "obsolete.py").write_text("VALUE = 0\n", encoding="utf-8")

            before = {path.name: path.read_bytes() for path in target.iterdir()}
            stale = synchronize(source, target, check=True)
            after = {path.name: path.read_bytes() for path in target.iterdir()}

            self.assertEqual(before, after)
            self.assertEqual(stale, ["__init__.py", "core.py", "obsolete.py"])

            synchronize(source, target, check=False)
            self.assertEqual(
                {path.name: path.read_bytes() for path in target.iterdir()},
                {path.name: path.read_bytes() for path in source.iterdir()},
            )

    def test_launcher_has_no_repo_source_fallback(self) -> None:
        launcher = (ROOT / "plugins" / "trading-desk" / "server.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('plugin_root / "runtime"', launcher)
        self.assertNotIn(' / "src"', launcher)
        self.assertNotIn("parents[2]", launcher)
        self.assertIn("outside the plugin runtime", launcher)

    def test_sync_refuses_a_symlinked_target_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            external = root / "external"
            source.mkdir()
            external.mkdir()
            (source / "__init__.py").write_text("# package\n", encoding="utf-8")
            linked_parent = root / "linked-runtime"
            linked_parent.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                synchronize(
                    source,
                    linked_parent / "trading_harness",
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
