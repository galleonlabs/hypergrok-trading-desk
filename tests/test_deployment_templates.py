from __future__ import annotations

from pathlib import Path
import plistlib
import re
import shlex
import unittest

from trading_harness.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]
LAUNCHD = ROOT / "deploy/launchd/com.jawndiego.trading-desk-research.plist.example"
SYSTEMD = ROOT / "deploy/systemd/trading-desk-research.service.example"
GUIDE = ROOT / "docs/always_on_operation.md"

PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
REQUIRED_PLACEHOLDERS = {
    "__REVIEWED_RESEARCH_USER__",
    "__REVIEWED_RESEARCH_GROUP__",
    "__REVIEWED_REPO_DIR__",
    "__REVIEWED_VENV_BIN__",
    "__REVIEWED_STATE_DIR__",
    "__REVIEWED_LOG_DIR__",
}
SAFE_RENDER = {
    "__REVIEWED_RESEARCH_USER__": "trading-research",
    "__REVIEWED_RESEARCH_GROUP__": "trading-research",
    "__REVIEWED_REPO_DIR__": "/opt/trading-desk/research",
    "__REVIEWED_VENV_BIN__": "/opt/trading-desk/research/.venv/bin",
    "__REVIEWED_STATE_DIR__": "/var/lib/trading-desk/research",
    "__REVIEWED_LOG_DIR__": "/var/log/trading-desk/research",
}


def template_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render(text: str, replacements: dict[str, str]) -> str:
    supplied = set(replacements)
    if supplied != REQUIRED_PLACEHOLDERS:
        raise ValueError("render requires the complete reviewed placeholder set")
    rendered = text
    for placeholder, value in replacements.items():
        if placeholder in {
            "__REVIEWED_RESEARCH_USER__",
            "__REVIEWED_RESEARCH_GROUP__",
        }:
            if (
                not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value)
                or value in {"root", "wheel"}
            ):
                raise ValueError("service identity is unsafe")
        else:
            path = Path(value)
            forbidden_roots = {
                Path("/tmp"),
                Path("/Users"),
                Path("/home"),
                Path("/root"),
                Path("/dev"),
                Path("/proc"),
                Path("/sys"),
            }
            if (
                not path.is_absolute()
                or path == Path("/")
                or ".." in path.parts
                or any(root == path or root in path.parents for root in forbidden_roots)
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("service path must be a narrow reviewed absolute path")
        rendered = rendered.replace(placeholder, value)
    if PLACEHOLDER_RE.search(rendered):
        raise ValueError("rendered service still contains a placeholder")
    return rendered


class SharedTemplateContractTests(unittest.TestCase):
    def test_templates_exist_and_use_only_the_reviewed_placeholders(self) -> None:
        for path in (LAUNCHD, SYSTEMD):
            self.assertTrue(path.is_file())
            text = template_text(path)
            found = set(PLACEHOLDER_RE.findall(text))
            self.assertEqual(found, REQUIRED_PLACEHOLDERS)
            self.assertTrue(all(value.startswith("__REVIEWED_") for value in found))

    def test_templates_contain_no_user_paths_shell_expansion_or_secrets(self) -> None:
        forbidden_paths = ("/Users/", "/home/", "/root/", "/tmp/", "~/", "$HOME")
        secret_pattern = re.compile(
            r"(?i)(private[_-]?key|api[_-]?key|bearer[_-]?token|wallet[_-]?secret)"
        )
        for path in (LAUNCHD, SYSTEMD):
            text = template_text(path)
            for forbidden in forbidden_paths:
                self.assertNotIn(forbidden, text)
            self.assertNotIn("${", text)
            self.assertNotIn("{{", text)
            self.assertNotRegex(text, secret_pattern)
            self.assertNotIn("--network", text)
            self.assertNotIn("mainnet", text.lower())
            self.assertNotIn("testnet", text.lower())

    def test_render_contract_rejects_unresolved_broad_or_relative_values(self) -> None:
        launchd = template_text(LAUNCHD)
        rendered = render(launchd, SAFE_RENDER)
        self.assertIsNone(PLACEHOLDER_RE.search(rendered))

        missing = dict(SAFE_RENDER)
        del missing["__REVIEWED_LOG_DIR__"]
        with self.assertRaisesRegex(ValueError, "complete"):
            render(launchd, missing)
        for dangerous in (
            "/",
            "/tmp",
            "/tmp/state",
            "/Users/example/state",
            "/home/example/state",
            "relative/path",
            "../state",
        ):
            replacements = dict(SAFE_RENDER)
            replacements["__REVIEWED_STATE_DIR__"] = dangerous
            with self.assertRaisesRegex(ValueError, "narrow reviewed absolute"):
                render(launchd, replacements)
        replacements = dict(SAFE_RENDER)
        replacements["__REVIEWED_RESEARCH_USER__"] = "root"
        with self.assertRaisesRegex(ValueError, "identity is unsafe"):
            render(launchd, replacements)

    def test_rendered_commands_match_the_current_cli_without_running_it(self) -> None:
        parser = build_parser()
        launchd = plistlib.loads(render(template_text(LAUNCHD), SAFE_RENDER).encode())
        launch_args = launchd["ProgramArguments"]
        parsed = parser.parse_args(launch_args[1:])
        self.assertEqual(parsed.command, "node")
        self.assertEqual(parsed.node_command, "run")
        self.assertEqual(str(parsed.state_db), "/var/lib/trading-desk/research/research.sqlite3")
        self.assertEqual(parsed.node_id, "trading-desk-research")
        self.assertEqual(parsed.poll_seconds, 1.0)
        self.assertEqual(parsed.history_bars, 1200)

        rendered_systemd = render(template_text(SYSTEMD), SAFE_RENDER)
        exec_start = next(
            line.removeprefix("ExecStart=")
            for line in rendered_systemd.splitlines()
            if line.startswith("ExecStart=")
        )
        systemd_args = shlex.split(exec_start)
        parsed = parser.parse_args(systemd_args[1:])
        self.assertEqual(parsed.command, "node")
        self.assertEqual(parsed.node_command, "run")
        self.assertEqual(str(parsed.state_db), "/var/lib/trading-desk/research/research.sqlite3")


class LaunchdTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = plistlib.loads(template_text(LAUNCHD).encode("utf-8"))

    def test_is_a_dedicated_fail_backoff_launchdaemon(self) -> None:
        self.assertEqual(
            self.payload["Label"], "com.jawndiego.trading-desk-research"
        )
        self.assertEqual(self.payload["UserName"], "__REVIEWED_RESEARCH_USER__")
        self.assertEqual(self.payload["GroupName"], "__REVIEWED_RESEARCH_GROUP__")
        self.assertTrue(self.payload["RunAtLoad"])
        self.assertEqual(self.payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertGreaterEqual(self.payload["ThrottleInterval"], 10)
        self.assertEqual(self.payload["Umask"], 0o77)
        self.assertEqual(self.payload["ProcessType"], "Background")

    def test_has_fixed_work_state_log_paths_and_no_environment_or_shell(self) -> None:
        arguments = self.payload["ProgramArguments"]
        self.assertEqual(
            arguments,
            [
                "__REVIEWED_VENV_BIN__/trading-harness",
                "node",
                "run",
                "--state-db",
                "__REVIEWED_STATE_DIR__/research.sqlite3",
                "--node-id",
                "trading-desk-research",
                "--poll-seconds",
                "1",
                "--history-bars",
                "1200",
            ],
        )
        self.assertEqual(self.payload["WorkingDirectory"], "__REVIEWED_REPO_DIR__")
        self.assertEqual(
            self.payload["StandardOutPath"],
            "__REVIEWED_LOG_DIR__/research.stdout.log",
        )
        self.assertEqual(
            self.payload["StandardErrorPath"],
            "__REVIEWED_LOG_DIR__/research.stderr.log",
        )
        self.assertNotIn("EnvironmentVariables", self.payload)
        self.assertNotIn("Program", self.payload)
        self.assertNotIn("/bin/sh", " ".join(arguments))


class SystemdTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = template_text(SYSTEMD)

    def test_uses_exact_node_command_and_supervisor_controls(self) -> None:
        self.assertIn("User=__REVIEWED_RESEARCH_USER__", self.text)
        self.assertIn("Group=__REVIEWED_RESEARCH_GROUP__", self.text)
        self.assertIn("WorkingDirectory=__REVIEWED_REPO_DIR__", self.text)
        self.assertIn(
            "ExecStart=__REVIEWED_VENV_BIN__/trading-harness node run "
            "--state-db __REVIEWED_STATE_DIR__/research.sqlite3 "
            "--node-id trading-desk-research --poll-seconds 1 --history-bars 1200",
            self.text,
        )
        self.assertIn("Restart=on-failure", self.text)
        self.assertIn("RestartSec=10s", self.text)
        self.assertIn("TimeoutStopSec=30s", self.text)
        self.assertIn("KillSignal=SIGTERM", self.text)
        self.assertIn("UMask=0077", self.text)
        self.assertNotIn("Environment=", self.text)
        self.assertNotIn("EnvironmentFile=", self.text)
        self.assertNotRegex(self.text, r"(?m)^Exec(Start|Stop)=.*(?:/bin/(?:ba)?sh|\s-c\s)")

    def test_limits_writes_and_process_privilege(self) -> None:
        for required in (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ReadWritePaths=__REVIEWED_STATE_DIR__ __REVIEWED_LOG_DIR__",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "LockPersonality=true",
            "RestrictSUIDSGID=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ):
            self.assertIn(required, self.text)
        self.assertIn(
            "StandardOutput=append:__REVIEWED_LOG_DIR__/research.stdout.log",
            self.text,
        )
        self.assertIn(
            "StandardError=append:__REVIEWED_LOG_DIR__/research.stderr.log",
            self.text,
        )


class OperatorGuideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = template_text(GUIDE)

    def test_documents_exact_current_cli_commands(self) -> None:
        required = (
            "./.venv/bin/trading-harness doctor",
            "trading-harness node run --state-db",
            "--node-id trading-desk-research --poll-seconds 1 --history-bars 1200",
            "trading-harness node status --state-db",
            "--node-id trading-desk-research",
        )
        for value in required:
            self.assertIn(value, self.text)

    def test_mac_first_and_linux_alternative_cover_supervision(self) -> None:
        mac = self.text.index("## macOS always-on computer")
        linux = self.text.index("## Linux/systemd alternative")
        self.assertLess(mac, linux)
        for required in (
            "launchctl bootstrap",
            "launchctl print",
            "launchctl kill SIGTERM",
            "launchctl bootout",
            "systemctl enable --now",
            "systemctl status",
            "systemctl kill --signal=SIGTERM",
            "Restart=on-failure",
            "ten-second restart delay",
        ):
            self.assertIn(required, self.text)

    def test_documents_security_separation_backup_recovery_and_clock(self) -> None:
        lower = self.text.lower()
        for required in (
            "research-only",
            "dedicated, non-administrator os identity",
            "separate deployment",
            "testnet and mainnet execution must use separate",
            "do not select mainnet with an environment variable",
            "mode `0700`",
            "`0600`",
            "distinct writable parents",
            "root-owned",
            "mode-`0400` signed-grant copy",
            "sqlite uses wal mode",
            '".backup',
            "pragma integrity_check",
            "rpo",
            "rto",
            "network-time",
            "timedatectl set-ntp true",
            "clock uncertainty",
        ):
            self.assertIn(required, lower)


if __name__ == "__main__":
    unittest.main()
