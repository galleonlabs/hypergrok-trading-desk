# HyperGrok Upstream Provenance

Status: current `main` and disconnected `v1.0.0` snapshot verified; complete remote ref mirror pending
Verified at: 2026-08-21T17:18:38Z

## Purpose

This record establishes chain of custody for inherited HyperGrok source material. Immutable upstream refs and archives are quarantined reference material inside this working fork. Harness branches contain the new product and must never load credentials from inherited source.

## Current main

| Field | Verified value |
| --- | --- |
| Upstream source | [`galleonlabs/hypergrok-trading-desk`](https://github.com/galleonlabs/hypergrok-trading-desk) |
| Audit fork | [`jawndiego/hypergrok-trading-desk`](https://github.com/jawndiego/hypergrok-trading-desk) |
| GitHub relationship | Fork `parent` and `source` both identify `galleonlabs/hypergrok-trading-desk` |
| Fork creation | `2026-08-21T17:06:48Z` |
| Default branch | `main` |
| Fork main commit | `62cbe227a2ec531e0efa37254d4b6fae043fbfe5` |
| Upstream main commit at verification | `62cbe227a2ec531e0efa37254d4b6fae043fbfe5` |
| Commit tree | `3b2fbd379c9956cde8be4038690ac1e94188e2b2` |
| Exact-commit archive | GitHub API tarball for `62cbe227a2ec531e0efa37254d4b6fae043fbfe5` |
| Archive SHA-256 | `5bc53e9b62845cb4f3871dec099ec9debe804739f3569e077c8106182b91bccc` |
| Latest commit signature | GitHub reports `verified: true` |
| Branch protection | `false` at verification |
| Tags present in fork | none |

Matching commit and tree IDs establish that fork `main` was byte-identical to upstream `main` at verification.

## Disconnected v1.0.0 capture

The GitHub fork did not copy upstream's disconnected `v1.0.0` tag. Upstream `v1.0.0` points at:

```text
53aae9fd7248a1854a3b591fd2b70ab9428b8a3b
```

| Field | Verified value |
| --- | --- |
| Tag commit | `53aae9fd7248a1854a3b591fd2b70ab9428b8a3b` |
| Commit tree | `179b58ef866c1f1156994d8081d5c7d1966de3aa` |
| Commit parents | none; independent root |
| Commit signature | GitHub reports `verified: false` / unsigned |
| Exact-commit archive SHA-256 | `ee8cc6663dc69c6a6f71dee99acaa0376bbb8583826eb40755f5689ed1a08df9` |

GitHub reports no common ancestor between that tag and current `main`. The tag contains a different Python implementation and must be audited as a separate product. Its exact snapshot is locally captured and hashed, but the working fork still lacks the audit ref.

## Quarantine requirements

- Keep production and personal trading credentials out of the fork, its Actions, environments, issues, logs, and local audit checkouts.
- Disable or independently inspect inherited Actions, hooks, apps, bots, deploy keys, environments, packages, and secrets before running anything. Public API access did not verify these settings.
- Protect the reference branch against accidental rewrites, or retain an independent immutable bundle of all verified objects.
- Do not auto-merge upstream changes. Import each change through a reviewed, provenance-recorded patch.
- Fetch mutable source only for comparison; build and audit from exact commit or object IDs.
- Audit every ref, tag, workflow, submodule, LFS object, release artifact, dependency, binary, command, prompt, skill, and factual API claim before reuse.

## Next provenance actions

1. Push an immutable audit ref for upstream `v1.0.0` without merging it into a harness branch.
2. Produce file manifests for both current `main` and the tagged Python lineage.
3. Create the source-to-derived disposition matrix: `retain`, `rewrite`, `reference_only`, or `reject`.
4. Protect audit refs and confirm that no inherited automation or credentials can execute from them.
