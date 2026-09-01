# Contributing

HyperGrok is instructions and resources for a user's Grok Bot, not software the repository runs. Contributions are prose, prompts and snippets; the bar is accuracy against the live Hyperliquid API and the live Grok Bot product.

## Ground rules

+ Original text only. Do not copy third-party skill prose, prompts, fixtures or code. Cite sources in `docs/PROVENANCE.md`.
+ No strategies, signals, alpha or return claims anywhere in the repository. The Strategist teaches method; users bring ideas.
+ Every write path stays behind the ticket protocol: Risk PASS, user approval by id, single bounded send, reconciliation. A change that weakens that is a rejected change.
+ Keep the desk's worked example consistent. `HG-20260816-01` runs through `README.md`, `desk-trade-lifecycle`, `desk-risk-limits`, `desk-monitoring`, `desk-post-trade-review`, `agents/risk-manager.md`, `agents/execution-trader.md` and `agents/trade-reviewer.md`. If you change its size, price or costs, change them everywhere and check the derived figures still hold: sizing is proportional, so the R multiple and the cost-in-bps should not move. The generic SDK snippets in `hyperliquid-orders` are not part of that narrative and use a round size on purpose.
+ Never widen the key model: API wallet only, through the secret store, never in chat or under `/workspace`.
+ Verify snippets against the current SDK versions named in `docs/PROVENANCE.md` before changing them, and update the version there if you bump it.

## Layout

+ `agents/<role>.md` - frontmatter (`name`, `title`, `description`, `seat`, `skills`, `writes_to_exchange`), a **Bot profile** section (Name, Job, Description) and a **System prompt** section.
+ `skills/<name>/SKILL.md` - frontmatter (`name` equal to the directory, `description` under 1024 characters, `license`, `metadata`), body under 320 lines.
+ `SETUP.md` - the single entry point a Grok Bot follows. Keep it linear and honest about what Grok Bot can and cannot do.

## Checks

```bash
bash scripts/check.sh
```

The check runs three passes and CI runs the same script:

+ Instruction files: frontmatter, directory/name agreement, description length, internal links and the one-writer rule.
+ `scripts/check_manifests.py` - the plugin distribution contract. Every manifest (`plugin.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.cursor-plugin/plugin.json`, `.grok-plugin/plugin.json`, `.grok-plugin/marketplace.json`) must be valid JSON, name the same plugin at the same version as `plugin.json`, point every declared source, logo, skills, agents and rules path at something that exists inside this repository, and claim the counts the repository actually ships. Adding or removing a skill or role means updating the manifests that state the count in prose. Any `/plugin marketplace add` or `/plugin install` command in the documentation must name the repository and the marketplace and plugin ids `.claude-plugin/marketplace.json` actually declares, so a rename cannot leave the install instructions pointing at nothing. A `skills add` argument or a skills.sh listing URL must name this repository or one of its skill directories; the plugin id `hypergrok` is not a skill, so a trailing `/hypergrok` on skills.sh is a missing listing.
+ `scripts/test_check_manifests.py` - negative fixtures that prove the manifest check fails on a version mismatch, a missing component path, invalid JSON, inventory drift, a documented install command that no longer resolves, a skills.sh listing or `skills add` argument that names a slug this repository does not ship, and a `SETUP.md` clone pin left behind by a version bump.

## Releasing

`SETUP.md` clones a **pinned tag**, so the desk a user builds is the desk that was reviewed. That pin drifts the moment `main` moves ahead of the tag, and a drifted pin is worse than none: the user reads one set of instructions and installs another. Cutting a release is therefore the only way changed instructions reach anyone, and these steps go together in one commit.

1. Move everything in `## Unreleased` in `CHANGELOG.md` under a new version heading with today's date. Say what changed and why, not just what moved.
2. Bump `version` in all six manifests to the new version. `check_manifests.py` fails if they disagree.
3. Bump `metadata.version` in the frontmatter of each skill whose body actually changed. Skills version independently; leave the untouched ones alone.
4. Update the `--branch` pin in `SETUP.md` section 1 to the new tag, and every other release tag the instruction files name. `check_manifests.py` fails if any of them disagree with the version from step 2; only `CHANGELOG.md` and this file may name older tags.
5. Run `bash scripts/check.sh`, then commit, then tag: `git tag -a vX.Y.Z -m "..." && git push origin main vX.Y.Z`.
6. Publish a GitHub release for the tag, and verify the published instructions actually work by running section 1 verbatim in an empty directory: clone the tag, then `bash scripts/check.sh` inside it.

The tag must contain a `SETUP.md` that pins to that same tag. If step 4 is skipped, the release ships instructions pointing at the previous one, so the check enforces it rather than leaving it to whoever cuts the release: step 2 without step 4 fails the build, and so does a pin that names a moving branch or no ref at all.

Version numbers are for this repository's instruction set, not for anything it installs. There is no `v1.0.0` tag: the 1.0.0 entry in the changelog is this markdown desk, while the tag of that name pointed at an earlier, unrelated Python CLI on a lineage that is not an ancestor of `main` (`53aae9f`). It was removed in 1.1.0. `v1.1.0` is the first tag that matches what this repository ships.
