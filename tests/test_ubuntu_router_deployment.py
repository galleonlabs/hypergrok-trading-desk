from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER_ROOT = ROOT / "deploy" / "ubuntu-router"
RENDERER_PATH = ROOT / "scripts" / "render_ubuntu_router.py"
ROUTER_GUIDE = ROOT / "docs" / "ubuntu_vm_router.md"
COMMISSIONING_GUIDE = ROOT / "docs" / "testnet_commissioning.md"
AGENTS = ROOT / "AGENTS.md"
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")

module_spec = importlib.util.spec_from_file_location(
    "render_ubuntu_router", RENDERER_PATH
)
assert module_spec is not None and module_spec.loader is not None
router_renderer = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(router_renderer)


def public_key(start: int) -> str:
    return base64.b64encode(bytes(range(start, start + 32))).decode("ascii")


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "local_nat_lab",
        "wan_interface": "enp0s1",
        "ingress_interface": "enp0s2",
        "management_source_cidr": "192.168.64.1/32",
        "router_endpoint_interface": "192.168.64.2/24",
        "listen_port": 51820,
        "router_ipv4_interface": "10.77.0.1/24",
        "mac_ipv4_peer": "10.77.0.2/32",
        "router_ipv6_interface": "fd77:77::1/64",
        "mac_ipv6_peer": "fd77:77::2/128",
        "dns_ipv4": "1.1.1.1",
        "router_public_key": public_key(1),
        "mac_public_key": public_key(33),
    }


class UbuntuRouterTemplateTests(unittest.TestCase):
    def test_templates_are_secret_free_local_lab_examples(self) -> None:
        expected = {
            "router-spec.json.example",
            "50-trading-desk-router.yaml.example",
            "wg-exec.conf.example",
            "nftables.conf.example",
            "70-trading-desk-router.conf.example",
            "mac-wireguard.conf.fragment.example",
            "trading-desk-router-check.sh.example",
        }
        self.assertEqual(expected, {path.name for path in ROUTER_ROOT.iterdir()})
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(ROUTER_ROOT.iterdir())
        )
        self.assertTrue(PLACEHOLDER_RE.findall(combined))
        self.assertTrue(
            all(
                value.startswith("__REVIEWED_")
                for value in PLACEHOLDER_RE.findall(combined)
            )
        )
        self.assertNotRegex(combined, r"(?im)^\s*PrivateKey\s*=")
        for forbidden in ("mainnet", "/Users/", "/home/", "/tmp/", "$HOME"):
            self.assertNotIn(forbidden, combined)

    def test_renderer_is_repository_script_not_runtime_capability(self) -> None:
        self.assertTrue(RENDERER_PATH.is_file())
        self.assertFalse((ROOT / "src/trading_harness/ubuntu_router.py").exists())
        text = RENDERER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "sudo",
            "ssh ",
            "launchctl",
            "PrivateKey =",
            "api_wallet",
            "/exchange",
        ):
            self.assertNotIn(forbidden, text)


class UbuntuRouterRendererTests(unittest.TestCase):
    def test_renders_deterministic_public_bundle_with_fail_closed_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "router.json"
            raw = (json.dumps(valid_spec(), indent=2, sort_keys=True) + "\n").encode()
            spec_path.write_bytes(raw)
            output = root / "rendered"

            manifest = router_renderer.render_bundle(spec_path, output)

            expected_files = {
                "wg-exec.conf",
                "50-trading-desk-router.yaml",
                "nftables.conf",
                "70-trading-desk-router.conf",
                "mac-wireguard.conf.fragment",
                "trading-desk-router-check",
                "bundle-manifest.json",
            }
            self.assertEqual(expected_files, {path.name for path in output.iterdir()})
            self.assertEqual(
                stat.S_IMODE(output.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE((output / "trading-desk-router-check").stat().st_mode),
                0o700,
            )
            for name in expected_files - {"trading-desk-router-check"}:
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)

            self.assertEqual(manifest["mode"], "local_nat_lab")
            self.assertEqual(manifest["source_spec_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(
                manifest["security_claims"],
                {
                    "changes_public_egress_ip": False,
                    "host_direct_bypass_prevented": False,
                    "mainnet_authorized": False,
                    "venue_writes_authorized": False,
                    "private_key_field_emitted": False,
                },
            )

            for name, expected_hash in manifest["files"].items():
                self.assertEqual(
                    hashlib.sha256((output / name).read_bytes()).hexdigest(),
                    expected_hash,
                )

            rendered_files = "\n".join(
                path.read_text(encoding="utf-8")
                for path in output.iterdir()
                if path.name != "bundle-manifest.json"
            )
            self.assertIsNone(PLACEHOLDER_RE.search(rendered_files))
            self.assertNotRegex(rendered_files, r"(?im)^\s*PrivateKey\s*=")
            self.assertNotIn("mainnet", rendered_files)

            wireguard = (output / "wg-exec.conf").read_text(encoding="utf-8")
            self.assertIn("Address = 10.77.0.1/24, fd77:77::1/64", wireguard)
            self.assertIn("AllowedIPs = 10.77.0.2/32, fd77:77::2/128", wireguard)
            self.assertIn(
                "private-key /etc/wireguard/trading-desk-router.key",
                wireguard,
            )

            netplan = (output / "50-trading-desk-router.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn('"enp0s1":', netplan)
            self.assertIn('"enp0s2":', netplan)
            self.assertIn("- 192.168.64.2/24", netplan)

            nftables = (output / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("chain forward", nftables)
            self.assertIn("policy drop", nftables)
            self.assertIn(
                'iifname "wg-exec" oifname "enp0s1" ip saddr 10.77.0.0/24 ip daddr 1.1.1.1 udp dport 53 ct state new,established accept',
                nftables,
            )
            self.assertIn(
                'iifname "wg-exec" oifname "enp0s1" ip saddr 10.77.0.0/24 tcp dport 443 ct state new,established accept',
                nftables,
            )
            self.assertIn(
                'iifname "enp0s1" udp sport 67 udp dport 68 accept',
                nftables,
            )
            self.assertIn(
                'iifname "enp0s1" oifname "wg-exec" ip daddr 10.77.0.0/24 ct state established,related accept',
                nftables,
            )
            self.assertNotIn('iifname "enp0s2" oifname "enp0s1"', nftables)
            self.assertNotIn("ip6 saddr", nftables)

            mac = (output / "mac-wireguard.conf.fragment").read_text(
                encoding="utf-8"
            )
            self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", mac)
            self.assertIn("Endpoint = 192.168.64.2:51820", mac)
            self.assertIn("PersistentKeepalive = 25", mac)
            check = (output / "trading-desk-router-check").read_text(
                encoding="utf-8"
            )
            self.assertIn("PATH=/usr/sbin:/usr/bin:/sbin:/bin", check)
            self.assertIn("trading_desk_nat postrouting", check)
            self.assertIn('wg show "$wg_interface" allowed-ips', check)
            self.assertIn("router_local_checks_passed", check)
            self.assertNotIn("router_ready", check)
            subprocess.run(
                ["/bin/sh", "-n", str(output / "trading-desk-router-check")],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest_digest = hashlib.sha256(
                (output / "bundle-manifest.json").read_bytes()
            ).hexdigest()
            self.assertEqual(
                router_renderer.verify_bundle(
                    output,
                    expected_manifest_sha256=manifest_digest,
                    require_owner_uid=os.getuid(),
                ),
                manifest,
            )
            hardlink = root / "wg-hardlink"
            os.link(output / "wg-exec.conf", hardlink)
            with self.assertRaisesRegex(ValueError, "link count"):
                router_renderer.verify_bundle(
                    output,
                    expected_manifest_sha256=manifest_digest,
                )
            hardlink.unlink()
            with (output / "wg-exec.conf").open("a", encoding="utf-8") as stream:
                stream.write("# tamper\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                router_renderer.verify_bundle(
                    output,
                    expected_manifest_sha256=manifest_digest,
                )
            malicious_manifest = json.loads(
                (output / "bundle-manifest.json").read_text(encoding="utf-8")
            )
            malicious_manifest["files"]["wg-exec.conf"] = hashlib.sha256(
                (output / "wg-exec.conf").read_bytes()
            ).hexdigest()
            (output / "bundle-manifest.json").write_text(
                json.dumps(malicious_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "retained value"):
                router_renderer.verify_bundle(
                    output,
                    expected_manifest_sha256=manifest_digest,
                )

    def test_rejects_invalid_or_ambiguous_public_topologies(self) -> None:
        cases: list[tuple[str, object]] = [
            ("schema_version", True),
            ("schema_version", 1.0),
            ("schema_version", 2),
            ("mode", "vpn_qualified"),
            ("wan_interface", "lo"),
            ("wan_interface", ".."),
            ("ingress_interface", "enp0s1"),
            ("management_source_cidr", "192.168.64.0/24"),
            ("management_source_cidr", "8.8.8.8/32"),
            ("management_source_cidr", "0.0.0.0/32"),
            ("management_source_cidr", "127.0.0.1/32"),
            ("management_source_cidr", "255.255.255.255/32"),
            ("router_endpoint_interface", "127.0.0.1/24"),
            ("router_endpoint_interface", "192.168.64.1/24"),
            ("router_endpoint_interface", "10.77.0.3/24"),
            ("router_endpoint_interface", "255.255.255.255/24"),
            ("router_endpoint_interface", "192.168.65.2/24"),
            ("listen_port", 80),
            ("router_ipv4_interface", "10.77.0.1/16"),
            ("router_ipv4_interface", "10.77.0.0/24"),
            ("router_ipv4_interface", "8.8.8.1/24"),
            ("mac_ipv4_peer", "10.78.0.2/32"),
            ("router_ipv6_interface", "2001:db8::1/64"),
            ("mac_ipv6_peer", "fd77:78::2/128"),
            ("dns_ipv4", "10.77.0.53"),
            ("dns_ipv4", "192.168.64.1"),
            ("router_public_key", base64.b64encode(bytes(32)).decode("ascii")),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                candidate = valid_spec()
                candidate[field] = value
                with self.assertRaises(ValueError):
                    router_renderer.validate_spec(candidate)

        candidate = valid_spec()
        candidate["extra"] = "not-reviewed"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "router.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "keys differ"):
                router_renderer._load_spec(path)

            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                router_renderer._load_spec(duplicate)

    def test_refuses_existing_output_or_symlinked_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_spec = root / "router.json"
            real_spec.write_text(json.dumps(valid_spec()), encoding="utf-8")
            linked_spec = root / "router-link.json"
            linked_spec.symlink_to(real_spec)
            with self.assertRaisesRegex(ValueError, "real regular"):
                router_renderer.render_bundle(linked_spec, root / "linked-output")

            output = root / "existing"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "must not already exist"):
                router_renderer.render_bundle(real_spec, output)


class UbuntuRouterDocumentationTests(unittest.TestCase):
    def test_agents_and_guides_preserve_router_and_testnet_boundaries(self) -> None:
        for path in (ROUTER_GUIDE, COMMISSIONING_GUIDE):
            self.assertTrue(path.is_file())
        agent_text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("docs/ubuntu_vm_router.md", agent_text)
        self.assertIn("docs/testnet_commissioning.md", agent_text)

        router = ROUTER_GUIDE.read_text(encoding="utf-8").lower()
        for required in (
            "local_nat_lab",
            "does not change the public ip",
            "does not prevent host bypass",
            "no api-wallet",
            "private key",
            "unknown outcome",
            "mainnet remains hard-disabled",
        ):
            self.assertIn(required, router)

        commissioning = COMMISSIONING_GUIDE.read_text(encoding="utf-8").lower()
        for required in (
            "macos security update",
            "quota",
            "pre-init acl",
            "post-init",
            "credential",
            "gtc",
            "websocket",
            "reduce-only close",
            "fault injection",
            "first harness order write remains blocked",
        ):
            self.assertIn(required, commissioning)


if __name__ == "__main__":
    unittest.main()
