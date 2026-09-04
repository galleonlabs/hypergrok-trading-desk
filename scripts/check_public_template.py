#!/usr/bin/env python3
"""Verify the public Grok Bot preview against the release manifest."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "template" / "grok-bot.json"


def fail(message: str) -> None:
    print(f"public template: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_preview(url: str, html_path: pathlib.Path | None) -> str:
    if html_path:
        return html_path.read_text(encoding="utf-8")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "hypergrok-public-template-check/1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def normalise(page: str) -> str:
    # Next.js serialises profile text inside JSON script tags. Decode the two
    # escaping layers without depending on its private hydration structure.
    decoded = html.unescape(page)
    return decoded.replace(r"\u0026", "&").replace(r'\"', '"')


def check(page: str, manifest: dict[str, object]) -> None:
    share_url = str(manifest["publicShareUrl"])
    match = re.fullmatch(r"https://x\.ai/bot/([A-Za-z0-9_-]+)", share_url)
    if not match:
        fail("manifest publicShareUrl is not a canonical x.ai bot URL")

    release = str(dict(manifest["source"])["release"])
    description = str(manifest["description"])
    required = {
        "name": str(manifest["name"]),
        "description": description,
        "Add to Grok Bot action": "Add to Grok Bot",
        "matching deep link": f"grokbot://app/v1/bot-template?id={match.group(1)}",
    }
    for label, value in required.items():
        if value not in page:
            fail(f"preview is missing {label}: {value!r}")

    if release not in description:
        fail(f"manifest description does not expose release {release}")
    print(f"ok: public Grok Bot preview matches {release} manifest and deep link")


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true", help="fetch the published preview")
    source.add_argument("--html", type=pathlib.Path, help="check a saved preview fixture")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    try:
        page = read_preview(str(manifest["publicShareUrl"]), args.html)
    except (OSError, UnicodeError) as error:
        fail(f"could not read preview: {error}")
    check(normalise(page), manifest)


if __name__ == "__main__":
    main()
