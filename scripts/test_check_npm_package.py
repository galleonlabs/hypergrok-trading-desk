#!/usr/bin/env python3
"""Package contract and artifact failure cases. Stdlib Python only; no network."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from check_npm_package import check, check_tarball, required_files

ROOT = Path(__file__).resolve().parents[1]


class PackageContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in required_files(ROOT, []):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        for relative in ("package.json", "plugin.json"):
            (self.root / relative).write_bytes((ROOT / relative).read_bytes())

    def edit(self, change):
        path = self.root / "package.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def fails(self, needle):
        errors, _ = check(self.root)
        self.assertTrue(errors, "broken package contract passed")
        self.assertIn(needle, "\n".join(errors))

    def test_valid_source(self):
        errors, expected = check(ROOT)
        self.assertEqual(errors, [])
        self.assertIn(".grok-plugin/marketplace.json", expected)
        self.assertIn(".agents/plugins/marketplace.json", expected)
        self.assertIn("gemini-extension.json", expected)
        self.assertIn("GEMINI.md", expected)
        self.assertIn("scripts/test_check_npm_package.py", expected)

    def test_wrong_name(self):
        self.edit(lambda data: data.update(name="hypergrok"))
        self.fails("name must be")

    def test_version_drift(self):
        self.edit(lambda data: data.update(version="9.9.9"))
        self.fails("version must match plugin.json")

    def test_invalid_version(self):
        self.edit(lambda data: data.update(version="main"))
        self.fails("stable semantic version")

    def test_license_drift(self):
        self.edit(lambda data: data.update(license="UNLICENSED"))
        self.fails("license must be MIT")

    def test_repository_drift(self):
        self.edit(lambda data: data["repository"].update(url="git+https://github.com/example/other.git"))
        self.fails("canonical GitHub repository")

    def test_registry_drift(self):
        self.edit(lambda data: data["publishConfig"].update(registry="https://example.com"))
        self.fails("public npm registry")

    def test_install_hook(self):
        self.edit(lambda data: data["scripts"].update(postinstall="python3 scripts/desk_doctor.py"))
        self.fails("install lifecycle hooks are forbidden")

    def test_changed_prepack(self):
        self.edit(lambda data: data["scripts"].update(prepack="echo ok"))
        self.fails("native test and prepack checks")

    def test_runtime_dependency(self):
        self.edit(lambda data: data.update(dependencies={"some-sdk": "*"}))
        self.fails("'dependencies'")

    def test_executable_entrypoint(self):
        self.edit(lambda data: data.update(bin={"hypergrok": "scripts/desk_doctor.py"}))
        self.fails("'bin'")

    def test_missing_skill_notice(self):
        next(self.root.glob("skills/*/LICENSE")).unlink()
        self.fails("is missing or is a symlink")

    def test_missing_hidden_manifest(self):
        self.edit(lambda data: data["files"].remove(".grok-plugin/marketplace.json"))
        self.fails(".grok-plugin/marketplace.json")

    def test_missing_test(self):
        self.edit(lambda data: data["files"].remove("scripts/test_desk_doctor.py"))
        self.fails("scripts/test_desk_doctor.py")

    def test_broad_allowlist(self):
        self.edit(lambda data: data["files"].append("skills/**"))
        self.fails("unapproved files or patterns")

    def test_secret_allowlist(self):
        self.edit(lambda data: data["files"].append(".env"))
        self.fails("unapproved files or patterns: .env")

    def test_duplicate_allowlist(self):
        self.edit(lambda data: data["files"].append("LICENSE"))
        self.fails("duplicate paths")

    def test_symlink(self):
        path = self.root / "assets/mascot.jpg"
        path.unlink()
        path.symlink_to("mascot-320.jpg")
        self.fails("is missing or is a symlink")

    def test_invalid_json(self):
        (self.root / "package.json").write_text("{", encoding="utf-8")
        self.fails("cannot read valid JSON")

    def test_non_string_allowlist(self):
        self.edit(lambda data: data["files"].append({"path": ".env"}))
        self.fails("explicit list of file paths")

    def artifact(self, omit=None, extra=None, symlink=None, duplicate=None):
        _, expected = check(self.root)
        path = self.root / "fixture.tgz"
        with tarfile.open(path, "w:gz") as archive:
            for relative in sorted(expected - {omit}):
                archive.add(self.root / relative, arcname=f"package/{relative}")
            if extra:
                member = tarfile.TarInfo(extra)
                body = b"fixture\n"
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            if symlink:
                member = tarfile.TarInfo(symlink)
                member.type = tarfile.SYMTYPE
                member.linkname = "/tmp/outside"
                archive.addfile(member)
            if duplicate:
                archive.add(self.root / duplicate, arcname=f"package/{duplicate}")
        return path, expected

    def test_valid_artifact(self):
        path, expected = self.artifact()
        self.assertEqual(check_tarball(path, expected), [])

    def test_artifact_missing_hidden_manifest(self):
        path, expected = self.artifact(omit=".claude-plugin/marketplace.json")
        self.assertIn("missing required files: .claude-plugin/marketplace.json", "\n".join(check_tarball(path, expected)))

    def test_artifact_unexpected_secret(self):
        path, expected = self.artifact(extra="package/.env")
        self.assertIn("unapproved file '.env'", "\n".join(check_tarball(path, expected)))

    def test_artifact_path_traversal(self):
        path, expected = self.artifact(extra="package/../.env")
        self.assertIn("unapproved file '../.env'", "\n".join(check_tarball(path, expected)))

    def test_artifact_symlink(self):
        path, expected = self.artifact(omit="LICENSE", symlink="package/LICENSE")
        self.assertIn("unexpected path or file type 'package/LICENSE'", "\n".join(check_tarball(path, expected)))

    def test_artifact_duplicate(self):
        path, expected = self.artifact(duplicate="LICENSE")
        self.assertIn("duplicate file 'LICENSE'", "\n".join(check_tarball(path, expected)))

    def test_artifact_stale_metadata(self):
        path, expected = self.artifact()
        self.edit(lambda data: data.update(version="9.9.9"))
        self.assertIn("'package.json' differs from the reviewed source", "\n".join(check_tarball(path, expected, self.root)))


if __name__ == "__main__":
    unittest.main()
