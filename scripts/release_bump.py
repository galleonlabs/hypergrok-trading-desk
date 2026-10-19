#!/usr/bin/env python3
"""Rewrite release pins and sync template skill hashes. Stdlib Python only.

Usage:
  python3 scripts/release_bump.py X.Y.Z --dry-run   show planned changes only
  python3 scripts/release_bump.py X.Y.Z             rewrite, sync hashes, run check.sh
  python3 scripts/release_bump.py                   sync template hashes, run check.sh

The old version is `version` in plugin.json. CHANGELOG, skill versions,
tags and publication stay manual. Ambiguous prose tags and backticked versions
block version-changing writes until manually reconciled; they are never rewritten.
Every mode refuses an existing target tag. Operate only on a trusted, quiescent
checkout or unpacked package: local check.sh is executed after writes.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import stat
from pathlib import Path

sys.dont_write_bytecode = True

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
    # A symlinked doc is an alias; it is never read or rewritten through the link.
    return [
        rel for rel in markdown_files(root)
        if rel not in RELEASE_HISTORY_DOCS and not os.path.islink(os.path.join(root, rel))
    ]


def safe_path(root, rel):
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise ValueError(f"unsafe path: {rel!r}")
    parts = rel.split("/")
    if os.path.isabs(rel) or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe path: {rel!r}")
    path = root
    for part in parts:
        path = os.path.join(path, part)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlink refused: {rel}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError(f"non-regular path refused: {rel}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError(f"hardlink refused: {rel}")
    if os.path.commonpath([root, os.path.realpath(path)]) != root:
        raise ValueError(f"path escapes root: {rel}")
    return path


def preflight(root):
    # Check all source paths before markdown discovery or check.sh can follow them.
    # Git metadata is deliberately excluded (worktrees use an external gitdir).
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in dirs + [f for f in files if f != ".git"]:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            if not os.path.islink(path):
                safe_path(root, rel)
            elif os.path.commonpath([root, os.path.realpath(path)]) != root:
                raise ValueError(f"symlink escapes root: {rel}")
    for rel in [*MANIFESTS, "package.json", TEMPLATE, "scripts/check.sh"]:
        safe_path(root, rel)
    for entry in json.loads(read(root, TEMPLATE)).get("skills") or []:
        safe_path(root, entry["path"])


def read(root, rel):
    with open(safe_path(root, rel), encoding="utf-8", newline="") as handle:
        return handle.read()


def write(root, rel, text):
    with open(safe_path(root, rel), "w", encoding="utf-8", newline="") as handle:
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

        (docs, re.compile(rf"hypergrok-trading-desk@(?P<ver>{SEMVER})(?!\w|\.\d)")),
        ([TEMPLATE], TAG_RE),
    ]
    for rel in docs:
        text = current(root, rel, pending)
        pending[rel] = TAG_RE.sub(
            lambda m: splice(m, "ver", new)
            if m["ver"] == old and PIN_BEFORE.search(text[:m.start()]) else m[0], text)
    for files, pattern in rules:
        for rel in files:
            pending[rel] = replace_version(pattern, current(root, rel, pending), old, new)


def digest(root, rel, pending):
    if rel in pending:
        return hashlib.sha256(pending[rel].encode("utf-8")).hexdigest()
    return sha256(safe_path(root, rel))


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
    # Unpacked npm archives have no Git metadata and remain usable offline.
    ancestors = [Path(root), *Path(root).parents]
    if not any(os.path.lexists(path / ".git") for path in ancestors):
        return False
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, check=False, env=env)
    location = git("rev-parse", "--show-toplevel")
    if location.returncode != 0 or os.path.realpath(location.stdout.strip()) != root:
        raise ValueError("cannot establish this root's Git repository; refusing mutation")
    found = git("show-ref", "--verify", "--quiet", f"refs/tags/v{version}")
    if found.returncode not in (0, 1):
        raise ValueError("cannot determine target tag state; refusing mutation")
    return found.returncode == 0


def manual_blockers(root, old):
    for rel in doc_files(root):
        for number, line in enumerate(read(root, rel).splitlines(), 1):
            if unpinned_tags(line, old) or re.search(rf"`{re.escape(old)}`", line):
                yield f"{rel}:{number}: ambiguous release claim requires manual reconciliation"


def main(argv):
    args = parse_args(argv)
    if args.version is not None and not VERSION_ARG_RE.fullmatch(args.version):
        print(f"version '{args.version}' must look like X.Y.Z", file=sys.stderr)
        return 2
    root = os.path.realpath(args.root)
    preflight(root)
    old = json.loads(read(root, MANIFESTS[0])).get("version")
    if not isinstance(old, str) or not VERSION_ARG_RE.fullmatch(old):
        print(f"{MANIFESTS[0]}: version must be a string like X.Y.Z, found {old!r}", file=sys.stderr)
        return 2
    if release_is_tagged(root, args.version or old):
        print(
            f"v{args.version or old} is already tagged; rewriting it would contradict the release; "
            "bump the version first",
            file=sys.stderr,
        )
        return 2
    if args.version == old:
        print(f"notice: plugin.json is already at {old}, so no version pin changes")
    blockers = list(manual_blockers(root, old)) if args.version and args.version != old else []
    if blockers:
        print("manual reconciliation required:")
        print("\n".join(blockers))
        if not args.dry_run:
            print("nothing written; reconcile factual claims without inventing evidence", file=sys.stderr)
            return 2
    pending = {}
    if args.version:
        rewrite(root, old, args.version, pending)
    sync_hashes(root, pending)
    validate_json(pending)
    pending = {rel: text for rel, text in pending.items() if text != read(root, rel)}
    report(root, old, args.version, pending, args.dry_run)
    if args.dry_run:
        return 0
    preflight(root)
    for rel, text in pending.items():
        write(root, rel, text)
    sys.stdout.flush()
    return subprocess.run(["bash", safe_path(root, "scripts/check.sh")], cwd=root).returncode


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        sys.exit(2)
