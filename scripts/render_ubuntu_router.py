#!/usr/bin/env python3
"""Render a private-key-field-free local Ubuntu router bundle.

The schema labels its two WireGuard inputs as public keys and emits no
``PrivateKey`` field. Public and private keys share an encoding, so the
renderer validates shape while an attended operator must verify provenance.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "deploy" / "ubuntu-router"
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")
INTERFACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")

SPEC_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "wan_interface",
        "ingress_interface",
        "management_source_cidr",
        "router_endpoint_interface",
        "listen_port",
        "router_ipv4_interface",
        "mac_ipv4_peer",
        "router_ipv6_interface",
        "mac_ipv6_peer",
        "dns_ipv4",
        "router_public_key",
        "mac_public_key",
    }
)

TEMPLATES: dict[str, tuple[str, int]] = {
    "50-trading-desk-router.yaml.example": (
        "50-trading-desk-router.yaml",
        0o600,
    ),
    "wg-exec.conf.example": ("wg-exec.conf", 0o600),
    "nftables.conf.example": ("nftables.conf", 0o600),
    "70-trading-desk-router.conf.example": (
        "70-trading-desk-router.conf",
        0o600,
    ),
    "mac-wireguard.conf.fragment.example": (
        "mac-wireguard.conf.fragment",
        0o600,
    ),
    "trading-desk-router-check.sh.example": (
        "trading-desk-router-check",
        0o700,
    ),
}

SECURITY_CLAIMS = {
    "changes_public_egress_ip": False,
    "host_direct_bypass_prevented": False,
    "mainnet_authorized": False,
    "venue_writes_authorized": False,
    "private_key_field_emitted": False,
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate router spec key: {key}")
        result[key] = value
    return result


def _is_rfc1918(address: ipaddress.IPv4Address) -> bool:
    return any(
        address in network
        for network in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
    )


def _read_regular_file(path: Path, *, maximum: int = 32 * 1024) -> bytes:
    if not path.is_absolute():
        raise ValueError("spec path must be absolute")
    if not path.is_file() or path.is_symlink():
        raise ValueError("spec must be a real regular file")
    size = path.stat().st_size
    if not 0 < size <= maximum:
        raise ValueError("spec size is invalid")
    return path.read_bytes()


def _public_key(value: object, field: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{field} must be a canonical WireGuard public key")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(
            f"{field} must be a canonical WireGuard public key"
        ) from error
    if (
        len(decoded) != 32
        or not any(decoded)
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise ValueError(f"{field} must be a canonical WireGuard public key")
    return value


def _interface(value: object, field: str) -> str:
    if not isinstance(value, str) or not INTERFACE_RE.fullmatch(value):
        raise ValueError(f"{field} must be a Linux interface name")
    if value in {".", "..", "lo", "wg-exec"}:
        raise ValueError(f"{field} collides with a reserved interface")
    return value


def _ipv4_address(value: object, field: str) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise ValueError(f"{field} must be an IPv4 address") from error
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or address.is_reserved
        or address == ipaddress.IPv4Address("255.255.255.255")
    ):
        raise ValueError(f"{field} is not a usable IPv4 address")
    return address


def _load_spec(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_file(path)
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("router spec must be canonical JSON") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) for key in decoded
    ):
        raise ValueError("router spec must be a JSON object")
    keys = frozenset(decoded)
    if keys != SPEC_KEYS:
        missing = sorted(SPEC_KEYS - keys)
        extra = sorted(keys - SPEC_KEYS)
        raise ValueError(f"router spec keys differ; missing={missing}, extra={extra}")
    return decoded, raw


def validate_spec(spec: dict[str, Any]) -> dict[str, str]:
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise ValueError("router schema_version must be exactly 1")
    if type(spec["mode"]) is not str or spec["mode"] != "local_nat_lab":
        raise ValueError("router mode must be exactly local_nat_lab")

    wan_interface = _interface(spec["wan_interface"], "wan_interface")
    ingress_interface = _interface(
        spec["ingress_interface"], "ingress_interface"
    )
    if wan_interface == ingress_interface:
        raise ValueError("WAN and ingress interfaces must be distinct")

    try:
        management_source = ipaddress.IPv4Network(
            spec["management_source_cidr"], strict=True
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "management_source_cidr must be one canonical IPv4 host CIDR"
        ) from error
    if management_source.prefixlen != 32:
        raise ValueError("management_source_cidr must be an IPv4 /32")
    if not _is_rfc1918(management_source.network_address):
        raise ValueError("management_source_cidr must be a private IPv4 /32")

    try:
        if not isinstance(spec["router_endpoint_interface"], str):
            raise ValueError
        endpoint_interface = ipaddress.IPv4Interface(
            spec["router_endpoint_interface"]
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "router_endpoint_interface must be a canonical IPv4 interface"
        ) from error
    endpoint = endpoint_interface.ip
    if not 24 <= endpoint_interface.network.prefixlen <= 30:
        raise ValueError("router endpoint network prefix must be from /24 to /30")
    if (
        not _is_rfc1918(endpoint)
        or endpoint in management_source
        or endpoint in {
            endpoint_interface.network.network_address,
            endpoint_interface.network.broadcast_address,
        }
        or management_source.network_address not in endpoint_interface.network
    ):
        raise ValueError(
            "router endpoint must be a usable private address sharing the management network"
        )
    dns = _ipv4_address(spec["dns_ipv4"], "dns_ipv4")
    if not dns.is_global:
        raise ValueError("dns_ipv4 must be one globally routable resolver address")

    port = spec["listen_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("listen_port must be an integer from 1024 to 65535")

    try:
        if not isinstance(spec["router_ipv4_interface"], str) or not isinstance(
            spec["mac_ipv4_peer"], str
        ):
            raise ValueError
        router_ipv4 = ipaddress.IPv4Interface(spec["router_ipv4_interface"])
        mac_ipv4 = ipaddress.IPv4Interface(spec["mac_ipv4_peer"])
    except (TypeError, ValueError) as error:
        raise ValueError("router and Mac IPv4 values must be canonical interfaces") from error
    if not 24 <= router_ipv4.network.prefixlen <= 30:
        raise ValueError("router IPv4 network prefix must be from /24 to /30")
    if not _is_rfc1918(router_ipv4.ip):
        raise ValueError("router IPv4 network must be RFC1918 private space")
    if router_ipv4.ip in {
        router_ipv4.network.network_address,
        router_ipv4.network.broadcast_address,
    }:
        raise ValueError("router IPv4 address cannot be the network or broadcast")
    if mac_ipv4.network.prefixlen != 32 or mac_ipv4.ip not in router_ipv4.network:
        raise ValueError("Mac IPv4 peer must be a /32 inside the router network")
    if mac_ipv4.ip in {
        router_ipv4.network.network_address,
        router_ipv4.network.broadcast_address,
    }:
        raise ValueError("Mac IPv4 peer cannot be the network or broadcast")
    if router_ipv4.ip == mac_ipv4.ip:
        raise ValueError("router and Mac IPv4 addresses must be distinct")
    if management_source.overlaps(router_ipv4.network):
        raise ValueError("management and WireGuard IPv4 networks must not overlap")
    if endpoint_interface.network.overlaps(router_ipv4.network) or dns in router_ipv4.network:
        raise ValueError("endpoint and DNS must be outside the WireGuard IPv4 network")

    try:
        if not isinstance(spec["router_ipv6_interface"], str) or not isinstance(
            spec["mac_ipv6_peer"], str
        ):
            raise ValueError
        router_ipv6 = ipaddress.IPv6Interface(spec["router_ipv6_interface"])
        mac_ipv6 = ipaddress.IPv6Interface(spec["mac_ipv6_peer"])
    except (TypeError, ValueError) as error:
        raise ValueError("router and Mac IPv6 values must be canonical interfaces") from error
    if router_ipv6.network.prefixlen != 64:
        raise ValueError("router IPv6 network prefix must be /64")
    if router_ipv6.ip not in ipaddress.IPv6Network("fc00::/7"):
        raise ValueError("router IPv6 interface must use a private ULA")
    if mac_ipv6.network.prefixlen != 128 or mac_ipv6.ip not in router_ipv6.network:
        raise ValueError("Mac IPv6 peer must be a /128 inside the router network")
    if router_ipv6.ip == mac_ipv6.ip:
        raise ValueError("router and Mac IPv6 addresses must be distinct")

    router_public_key = _public_key(
        spec["router_public_key"], "router_public_key"
    )
    mac_public_key = _public_key(spec["mac_public_key"], "mac_public_key")
    if router_public_key == mac_public_key:
        raise ValueError("router and Mac public keys must be distinct")

    replacements = {
        "__REVIEWED_WAN_INTERFACE__": wan_interface,
        "__REVIEWED_INGRESS_INTERFACE__": ingress_interface,
        "__REVIEWED_MANAGEMENT_SOURCE_CIDR__": str(management_source),
        "__REVIEWED_ROUTER_ENDPOINT_IP__": str(endpoint),
        "__REVIEWED_ROUTER_ENDPOINT_INTERFACE__": str(endpoint_interface),
        "__REVIEWED_LISTEN_PORT__": str(port),
        "__REVIEWED_ROUTER_IPV4_INTERFACE__": str(router_ipv4),
        "__REVIEWED_ROUTER_IPV4_NETWORK__": str(router_ipv4.network),
        "__REVIEWED_MAC_IPV4_PEER__": str(mac_ipv4),
        "__REVIEWED_ROUTER_IPV6_INTERFACE__": str(router_ipv6),
        "__REVIEWED_MAC_IPV6_PEER__": str(mac_ipv6),
        "__REVIEWED_DNS_IPV4__": str(dns),
        "__REVIEWED_ROUTER_ENDPOINT__": f"{endpoint}:{port}",
        "__REVIEWED_ROUTER_PUBLIC_KEY__": router_public_key,
        "__REVIEWED_MAC_PUBLIC_KEY__": mac_public_key,
    }
    return replacements


def _render_template(template_path: Path, replacements: dict[str, str]) -> bytes:
    text = template_path.read_text(encoding="utf-8")
    placeholders = frozenset(PLACEHOLDER_RE.findall(text))
    missing = placeholders - replacements.keys()
    if missing:
        raise ValueError(
            f"template {template_path.name} has unknown placeholders: {sorted(missing)}"
        )
    for placeholder in placeholders:
        text = text.replace(placeholder, replacements[placeholder])
    if PLACEHOLDER_RE.search(text):
        raise ValueError(f"template {template_path.name} remains unresolved")
    if re.search(r"(?im)^\s*PrivateKey\s*=", text):
        raise ValueError(f"template {template_path.name} emitted a private key field")
    for forbidden in ("mainnet", "/Users/", "/home/", "/tmp/", "$HOME"):
        if forbidden in text:
            raise ValueError(
                f"template {template_path.name} contains forbidden text: {forbidden}"
            )
    return text.encode("utf-8")


def _write_file(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    path.chmod(mode)


def render_bundle(spec_path: Path, output_dir: Path) -> dict[str, Any]:
    spec, raw_spec = _load_spec(spec_path)
    replacements = validate_spec(spec)

    if not output_dir.is_absolute():
        raise ValueError("output directory must be absolute")
    # Normalize platform aliases such as macOS /var -> /private/var. The
    # The bundle emits no PrivateKey field; the final root install has its own
    # owner/mode checks. Public/private WireGuard strings are otherwise not
    # distinguishable, so public-key provenance remains an operator duty.
    output_dir = output_dir.resolve(strict=False)
    if output_dir.exists():
        raise ValueError("output directory must not already exist")
    parent = output_dir.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("output parent must be a real existing directory")

    rendered: dict[str, tuple[bytes, int]] = {}
    for template_name, (output_name, mode) in TEMPLATES.items():
        template_path = TEMPLATE_ROOT / template_name
        if not template_path.is_file() or template_path.is_symlink():
            raise ValueError(f"router template is missing or unsafe: {template_name}")
        rendered[output_name] = (
            _render_template(template_path, replacements),
            mode,
        )

    try:
        output_dir.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ValueError("output directory must not already exist") from error
    try:
        file_hashes: dict[str, str] = {}
        for output_name, (content, mode) in sorted(rendered.items()):
            _write_file(output_dir / output_name, content, mode)
            file_hashes[output_name] = _sha256(content)

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "bundle_kind": "trading-desk.local-ubuntu-router",
            "mode": "local_nat_lab",
            "source_spec_sha256": _sha256(raw_spec),
            "security_claims": SECURITY_CLAIMS,
            "files": file_hashes,
        }
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _write_file(output_dir / "bundle-manifest.json", manifest_bytes, 0o600)
    except BaseException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return manifest


def verify_bundle(
    bundle_dir: Path,
    *,
    expected_manifest_sha256: str,
    require_owner_uid: int | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise ValueError("expected manifest SHA-256 is invalid")
    if require_owner_uid is not None and (
        type(require_owner_uid) is not int or require_owner_uid < 0
    ):
        raise ValueError("required owner UID is invalid")
    if not bundle_dir.is_absolute():
        raise ValueError("bundle directory must be absolute")
    if bundle_dir.is_symlink():
        raise ValueError("bundle must not be a symlink")
    bundle_dir = bundle_dir.resolve(strict=False)
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise ValueError("bundle must be a real directory")
    if stat.S_IMODE(bundle_dir.stat().st_mode) != 0o700:
        raise ValueError("bundle directory mode must be 0700")
    if bundle_dir.stat().st_nlink < 1:
        raise ValueError("bundle directory link count is invalid")
    if require_owner_uid is not None and bundle_dir.stat().st_uid != require_owner_uid:
        raise ValueError("bundle directory owner is invalid")

    expected_modes = {
        output_name: mode for output_name, mode in TEMPLATES.values()
    }
    expected_modes["bundle-manifest.json"] = 0o600
    entries = {path.name: path for path in bundle_dir.iterdir()}
    if set(entries) != set(expected_modes):
        raise ValueError("bundle file set differs from the rendered contract")
    for name, expected_mode in expected_modes.items():
        path = entries[name]
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"bundle entry is not a regular file: {name}")
        if path.stat().st_nlink != 1:
            raise ValueError(f"bundle entry link count is invalid: {name}")
        if require_owner_uid is not None and path.stat().st_uid != require_owner_uid:
            raise ValueError(f"bundle entry owner is invalid: {name}")
        if stat.S_IMODE(path.stat().st_mode) != expected_mode:
            raise ValueError(f"bundle entry mode is invalid: {name}")
        if not 0 < path.stat().st_size <= 1024 * 1024:
            raise ValueError(f"bundle entry size is invalid: {name}")

    manifest_bytes = entries["bundle-manifest.json"].read_bytes()
    if _sha256(manifest_bytes) != expected_manifest_sha256:
        raise ValueError("bundle manifest digest differs from the retained value")
    try:
        manifest = json.loads(
            manifest_bytes,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("bundle manifest is invalid JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "bundle_kind",
        "mode",
        "source_spec_sha256",
        "security_claims",
        "files",
    }:
        raise ValueError("bundle manifest keys differ from the contract")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("bundle manifest schema is invalid")
    if manifest["bundle_kind"] != "trading-desk.local-ubuntu-router":
        raise ValueError("bundle manifest kind is invalid")
    if manifest["mode"] != "local_nat_lab":
        raise ValueError("bundle manifest mode is invalid")
    if manifest["security_claims"] != SECURITY_CLAIMS:
        raise ValueError("bundle security claims differ from the contract")
    source_hash = manifest["source_spec_sha256"]
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("bundle source spec hash is invalid")

    files = manifest["files"]
    expected_hashed_files = set(expected_modes) - {"bundle-manifest.json"}
    if not isinstance(files, dict) or set(files) != expected_hashed_files:
        raise ValueError("bundle manifest file hashes differ from the contract")
    for name, expected_hash in files.items():
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise ValueError(f"bundle file hash is invalid: {name}")
        if _sha256(entries[name].read_bytes()) != expected_hash:
            raise ValueError(f"bundle file hash mismatch: {name}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a private-key-field-free local Ubuntu router bundle"
    )
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check-bundle", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--require-owner-uid", type=int)
    arguments = parser.parse_args(argv)
    if arguments.check_bundle is not None:
        if arguments.spec is not None or arguments.output_dir is not None:
            parser.error("--check-bundle cannot be combined with render arguments")
        if arguments.expected_manifest_sha256 is None:
            parser.error("--expected-manifest-sha256 is required with --check-bundle")
        try:
            manifest = verify_bundle(
                arguments.check_bundle,
                expected_manifest_sha256=arguments.expected_manifest_sha256,
                require_owner_uid=arguments.require_owner_uid,
            )
        except (OSError, ValueError) as error:
            print(f"Ubuntu router bundle check failed: {error}", file=sys.stderr)
            return 2
        print(f"verified Ubuntu router bundle: {arguments.check_bundle}")
        print(f"source spec sha256: {manifest['source_spec_sha256']}")
        return 0
    if arguments.spec is None or arguments.output_dir is None:
        parser.error("--spec and --output-dir are required when rendering")
    if (
        arguments.expected_manifest_sha256 is not None
        or arguments.require_owner_uid is not None
    ):
        parser.error("bundle verification arguments cannot be used when rendering")
    try:
        manifest = render_bundle(arguments.spec, arguments.output_dir)
    except (OSError, ValueError) as error:
        print(f"Ubuntu router render failed: {error}", file=sys.stderr)
        return 2
    manifest_path = arguments.output_dir / "bundle-manifest.json"
    print(f"rendered Ubuntu router bundle: {arguments.output_dir}")
    print(f"bundle manifest: {manifest_path}")
    print(f"bundle manifest sha256: {_sha256(manifest_path.read_bytes())}")
    print(f"source spec sha256: {manifest['source_spec_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
