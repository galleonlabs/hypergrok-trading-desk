#!/usr/bin/env python3
"""Validate the public Grok Bot template contract. Stdlib Python only."""

import hashlib
import json
import os
import re
import sys


REPOSITORY = "https://github.com/galleonlabs/hypergrok-trading-desk"
TEMPLATE = "template/grok-bot.json"
PUBLIC_URL_RE = re.compile(r"https://x\.ai/bot/[A-Za-z0-9_-]+$")


def load_json(path, errors):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{os.path.relpath(path)}: cannot load JSON: {exc}")
        return {}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(root):
    errors = []
    manifest = load_json(os.path.join(root, "plugin.json"), errors)
    template = load_json(os.path.join(root, TEMPLATE), errors)
    version = manifest.get("version")
    expected_release = f"v{version}"

    if template.get("schemaVersion") != 1:
        errors.append(f"{TEMPLATE}: schemaVersion must be 1")
    if template.get("name") != "HyperGrok Desk Lead":
        errors.append(f"{TEMPLATE}: name must be 'HyperGrok Desk Lead'")
    if template.get("startMessage") != "Start the desk.":
        errors.append(f"{TEMPLATE}: startMessage must be 'Start the desk.'")
    description = template.get("description")
    if not isinstance(description, str) or not description:
        errors.append(f"{TEMPLATE}: description is required")
    elif len(description) > 280:
        errors.append(f"{TEMPLATE}: description is {len(description)} chars (limit 280)")
    elif not all(term in description.lower() for term in ("research-only", "no wallet", "order")):
        errors.append(f"{TEMPLATE}: description must state the public safety boundary")

    source = template.get("source") or {}
    if source.get("repository") != REPOSITORY:
        errors.append(f"{TEMPLATE}: source.repository must name this repository")
    if source.get("release") != expected_release:
        errors.append(
            f"{TEMPLATE}: source.release '{source.get('release')}' does not match {expected_release}"
        )
    expected_urls = {
        "bootstrap": f"{REPOSITORY}/blob/{expected_release}/skills/hypergrok-bootstrap/SKILL.md",
        "runbook": f"{REPOSITORY}/blob/{expected_release}/SETUP.md",
    }
    for key, expected in expected_urls.items():
        if source.get(key) != expected:
            errors.append(f"{TEMPLATE}: source.{key} must be {expected}")

    avatar = template.get("avatar")
    if not isinstance(avatar, str) or not os.path.isfile(os.path.join(root, avatar)):
        errors.append(f"{TEMPLATE}: avatar must resolve to a repository file")

    expected_skill_names = sorted(
        name
        for name in os.listdir(os.path.join(root, "skills"))
        if os.path.isfile(os.path.join(root, "skills", name, "SKILL.md"))
    )
    entries = template.get("skills")
    if not isinstance(entries, list):
        errors.append(f"{TEMPLATE}: skills must be a list")
        entries = []
    names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
    if names != expected_skill_names:
        errors.append(
            f"{TEMPLATE}: skills must list exactly the {len(expected_skill_names)} repository skills in name order"
        )
    if len(names) != len(set(names)):
        errors.append(f"{TEMPLATE}: duplicate skill names are not allowed")

    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"{TEMPLATE}: each skill entry must be an object")
            continue
        name = entry.get("name")
        expected_path = f"skills/{name}/SKILL.md"
        if entry.get("path") != expected_path:
            errors.append(f"{TEMPLATE}: {name} path must be {expected_path}")
            continue
        path = os.path.join(root, expected_path)
        if not os.path.isfile(path):
            errors.append(f"{TEMPLATE}: {name} points at a missing file")
        elif entry.get("sha256") != sha256(path):
            errors.append(f"{TEMPLATE}: {name} sha256 does not match {expected_path}")

    for capability in ("plugins", "memories", "routines"):
        if template.get(capability) != []:
            errors.append(f"{TEMPLATE}: public bootstrap {capability} must be empty")

    status = template.get("status")
    public_url = template.get("publicShareUrl")
    if status == "ready-to-publish":
        if public_url is not None:
            errors.append(f"{TEMPLATE}: ready-to-publish template must not carry a publicShareUrl")
    elif status == "published":
        if not isinstance(public_url, str) or not PUBLIC_URL_RE.fullmatch(public_url):
            errors.append(f"{TEMPLATE}: published template needs an https://x.ai/bot/<id> URL")
        else:
            for rel in ("README.md", "docs/FAQ.md"):
                with open(os.path.join(root, rel), encoding="utf-8") as handle:
                    if public_url not in handle.read():
                        errors.append(f"{rel}: published template URL is missing")
    else:
        errors.append(f"{TEMPLATE}: status must be ready-to-publish or published")

    bootstrap_path = os.path.join(root, "skills", "hypergrok-bootstrap", "SKILL.md")
    with open(bootstrap_path, encoding="utf-8") as handle:
        bootstrap = handle.read()
    for phrase in ("do not create duplicate skills", "`template`", "exactly seventeen unique skill names"):
        if phrase not in bootstrap:
            errors.append(f"skills/hypergrok-bootstrap/SKILL.md: missing template idempotency rule '{phrase}'")

    if errors:
        print("\n".join(errors))
        print(f"{len(errors)} problem(s)")
        return 1
    print(f"ok: Grok Bot template is {status}, pinned to {expected_release}, with {len(entries)} verified skills")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
