#!/usr/bin/env python3
"""Rewrite release pins and sync template skill hashes. Stdlib Python only.

Usage:
  python3 scripts/release_bump.py X.Y.Z --dry-run   show planned changes only
  python3 scripts/release_bump.py X.Y.Z             rewrite, sync hashes, run check.sh
  python3 scripts/release_bump.py                   sync template hashes, run check.sh

The old version is `version` in plugin.json. CHANGELOG, skill
`metadata.version`, tag and push stay manual. Bare versions in prose are
listed for review, not rewritten, because they are evidence claims. A prose
`vX.Y.Z` is rewritten because the checker requires it, then listed too unless
it follows a pin context such as `--branch` or `@`. Hash-only mode is for the
window between the version bump and the tag, and refuses once `vX.Y.Z` exists.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

from check_grok_template import TEMPLATE, sha256
from check_manifests import MANIFESTS, RELEASE_HISTORY_DOCS, markdown_files

SEMVER = r"\d+\.\d+\.\d+"
VERSION_ARG_RE = re.compile(SEMVER)
# Bounds that reject 11.6.1, 1.6.10 and 1.6.1.2 but allow a sentence-final period.
TAG_RE = re.compile(rf"(?<![\w.])v(?P<ver>{SEMVER})(?!\w|\.\d)")
BARE_RE = re.compile(rf"(?<![\w.`])(?P<ver>{SEMVER})(?!\w|\.\d|`)")
# A tag after one of these is a pin, not a claim that the release exists.
PIN_BEFORE = re.compile(r"(?:--branch[= ]|@|/(?:blob|tree|raw)/|refs/tags/)\Z")
HASH_RE = re.compile(
    r'"path"\s*:\s*"(?P<path>[^"]+)"\s*,\s*"sha256"\s*:\s*"(?P<hash>[0-9a-f]*)"'
)
SNIPPET = 70
LINE_LIMIT = 160


def doc_files(root):
    return [rel for rel in markdown_files(root) if rel not in RELEASE_HISTORY_DOCS]


def read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8", newline="") as handle:
        return handle.read()


def write(root, rel, text):
    with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def splice(match, group, value):
    start, end = (i - match.start() for i in match.span(group))
    return match[0][:start] + value + match[0][end:]


def replace_version(pattern, text, old, new):
    return pattern.sub(lambda m: splice(m, "ver", new) if m["ver"] == old else m[0], text)


def current(root, rel, pending):
    return pending[rel] if rel in pending else read(root, rel)


def rewrite(root, old, new, pending):
    manifests = [*MANIFESTS, "package.json"]
    docs = doc_files(root)
    rules = [
        (manifests, re.compile(rf'"version"\s*:\s*"(?P<ver>{SEMVER})"')),
        (manifests, re.compile(rf'"ref"\s*:\s*"v(?P<ver>{SEMVER})"')),
        (docs, TAG_RE),
        (docs, re.compile(rf"`(?P<ver>{SEMVER})`")),
        (docs, re.compile(rf"hypergrok-trading-desk@(?P<ver>{SEMVER})(?!\w|\.\d)")),
        ([TEMPLATE], TAG_RE),
    ]
    for files, pattern in rules:
        for rel in files:
            pending[rel] = replace_version(pattern, current(root, rel, pending), old, new)


def digest(root, rel, pending):
    if rel in pending:
        return hashlib.sha256(pending[rel].encode("utf-8")).hexdigest()
    return sha256(os.path.join(root, rel))


def sync_hashes(root, pending):
    text = current(root, TEMPLATE, pending)
    synced = HASH_RE.sub(lambda m: splice(m, "hash", digest(root, m["path"], pending)), text)
    for entry in json.loads(synced).get("skills") or []:
        if entry.get("sha256") != digest(root, entry["path"], pending):
            sys.exit(f"{TEMPLATE}: could not sync the sha256 of {entry['path']}")
    pending[TEMPLATE] = synced


def snippet(old_line, new_line):
    start = max(0, len(os.path.commonprefix([old_line, new_line])) - 20)
    before, after = (s[start:start + SNIPPET].strip() for s in (old_line, new_line))
    return f"{before} -> {after}"


def changed_lines(root, pending):
    for rel in sorted(pending):
        old_lines = read(root, rel).splitlines()
        new_lines = pending[rel].splitlines()
        for number, (before, after) in enumerate(zip(old_lines, new_lines), 1):
            if before != after:
                yield rel, f"{rel}:{number}: {snippet(before, after)}"[:LINE_LIMIT]


def unpinned_tags(line, new):
    return [
        m.start() for m in TAG_RE.finditer(line)
        if m["ver"] == new and not PIN_BEFORE.search(line[:m.start()])
    ]


def review_lines(root, old, new, pending):
    for rel in doc_files(root):
        text = current(root, rel, pending)
        for number, line in enumerate(text.splitlines(), 1):
            hits = [m.start() for m in BARE_RE.finditer(line) if m["ver"] == old]
            hits += unpinned_tags(line, new)
            if hits:
                start = max(0, min(hits) - 60)
                context = ("..." if start else "") + line[start:].strip()
                yield f"{rel}:{number}: {context}"[:LINE_LIMIT]


def validate_json(pending):
    for rel, text in pending.items():
        if rel.endswith(".json"):
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                sys.exit(f"{rel}: rewrite broke JSON: {exc}")


def report(root, old, new, pending, dry_run):
    changes = list(changed_lines(root, pending))
    for _, line in changes:
        print(line)
    review = list(review_lines(root, old, new, pending)) if new else []
    if review:
        print("review by hand:")
        for line in review:
            print(f"  {line}")
    files = len({rel for rel, _ in changes})
    print(f"{len(changes)} changed line(s) in {files} file(s), {len(review)} line(s) to review by hand")
    if dry_run:
        print("dry run, nothing written")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help="new release version, X.Y.Z")
    parser.add_argument("--dry-run", action="store_true", help="print planned changes only")
    parser.add_argument(
        "--root",
        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        help="repository root (default: this script's repository)",
    )
    return parser.parse_args(argv)


def release_is_tagged(root, version):
    try:
        found = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "--quiet", f"refs/tags/v{version}"],
            capture_output=True, check=False,
        )
    except OSError:
        return False
    return found.returncode == 0


def main(argv):
    args = parse_args(argv)
    if args.version is not None and not VERSION_ARG_RE.fullmatch(args.version):
        print(f"version '{args.version}' must look like X.Y.Z", file=sys.stderr)
        return 2
    root = os.path.abspath(args.root)
    old = json.loads(read(root, MANIFESTS[0])).get("version")
    if not isinstance(old, str) or not VERSION_ARG_RE.fullmatch(old):
        print(f"{MANIFESTS[0]}: version must be a string like X.Y.Z, found {old!r}", file=sys.stderr)
        return 2
    if args.version is None and release_is_tagged(root, old):
        print(
            f"v{old} is already tagged, so a hash-only sync would contradict the release; "
            "bump the version first",
            file=sys.stderr,
        )
        return 2
    if args.version == old:
        print(f"notice: plugin.json is already at {old}, so no version pin changes")
    pending = {}
    if args.version:
        rewrite(root, old, args.version, pending)
    sync_hashes(root, pending)
    validate_json(pending)
    pending = {rel: text for rel, text in pending.items() if text != read(root, rel)}
    report(root, old, args.version, pending, args.dry_run)
    if args.dry_run:
        return 0
    for rel, text in pending.items():
        write(root, rel, text)
    sys.stdout.flush()
    return subprocess.run(["bash", os.path.join(root, "scripts", "check.sh")]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
