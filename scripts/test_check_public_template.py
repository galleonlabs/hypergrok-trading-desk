#!/usr/bin/env python3
"""Public profile checks remain testable while a new template is awaiting publication."""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
from check_public_template import check, normalise

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'template/grok-bot.json').read_text())
# A fixture identity independent of the real template's current publication state.
FIXTURE = {**MANIFEST, 'status': 'published', 'publicShareUrl': 'https://x.ai/bot/test-profile'}
BOT_ID = 'test-profile'


def run(page):
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
        try:
            check(normalise(page), FIXTURE)
            return 0, errors.getvalue()
        except SystemExit as error:
            return error.code, errors.getvalue()


valid = (
    f"<title>{FIXTURE['name']}</title>"
    f"<meta content=\"{FIXTURE['description']}\">"
    '<button>Add to Grok Bot</button>'
    f'<a href="grokbot://app/v1/bot-template?id={BOT_ID}">Add</a>'
)
assert run(valid)[0] == 0
for label, stale in (
    ('name', valid.replace(FIXTURE['name'], 'Other Bot')),
    ('description', valid.replace(FIXTURE['description'], 'Old release')),
    ('action', valid.replace('Add to Grok Bot', 'Install')),
    ('deep link', valid.replace(BOT_ID, 'wrong-id')),
):
    code, message = run(stale)
    assert code == 1, f'{label} drift unexpectedly passed'
    assert 'public template:' in message

if MANIFEST['status'] == 'ready-to-publish':
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_public_template.py'), '--live'],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert 'publication is still pending' in result.stderr
    assert 'Traceback' not in result.stderr

print('ok: public template checker positive, drift and pending-publication fixtures')
