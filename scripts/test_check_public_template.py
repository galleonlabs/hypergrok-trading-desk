#!/usr/bin/env python3
"""Negative and positive fixtures for check_public_template.py."""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_public_template.py"
MANIFEST = json.loads((ROOT / "template" / "grok-bot.json").read_text(encoding="utf-8"))
BOT_ID = MANIFEST["publicShareUrl"].rsplit("/", 1)[1]


def run(page: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8") as fixture:
        fixture.write(page)
        fixture.flush()
        return subprocess.run(
            ["python3", str(CHECK), "--html", fixture.name],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )


valid = (
    f"<title>{MANIFEST['name']}</title>"
    f"<meta content=\"{MANIFEST['description']}\">"
    "<button>Add to Grok Bot</button>"
    f"<a href=\"grokbot://app/v1/bot-template?id={BOT_ID}\">Add</a>"
)

assert run(valid).returncode == 0
for label, stale in (
    ("name", valid.replace(MANIFEST["name"], "Other Bot")),
    ("description", valid.replace(MANIFEST["description"], "Old release")),
    ("action", valid.replace("Add to Grok Bot", "Install")),
    ("deep link", valid.replace(BOT_ID, "wrong-id")),
):
    result = run(stale)
    assert result.returncode == 1, f"{label} drift unexpectedly passed"
    assert "public template:" in result.stderr

print("ok: public template checker positive and drift fixtures")
