# Contributing

HyperGrok is instructions and resources that a user's agents follow, plus read-only tools. The bar for a change is accuracy against the live Hyperliquid API and the live agent runtimes.

## Ground rules

+ **Original text only.** Do not copy third-party skill prose, prompts, fixtures or code. Cite sources in `docs/PROVENANCE.md`.
+ **No strategies, signals or return claims** anywhere. The Strategist teaches method; users bring ideas.
+ **Every write stays behind the ticket protocol:** Risk PASS, user approval by id, one bounded send, reconciliation. The one alternative to approval by id is a standing mandate the user writes themselves (`docs/STANDING_MANDATE.md`), honoured only where an out-of-band check reads it. A change that weakens this, lets the desk write or widen a mandate, or honours a mandate in a runtime without such a check is rejected.
+ **Never widen the key model:** a trade-only API wallet, through the secret store, never in chat or under `/workspace`.
+ **Keep the worked example consistent.** `HG-20260816-01` runs through `README.md`, `docs/STOP_COST.md`, `desk-trade-lifecycle`, `desk-risk-limits`, `desk-monitoring`, `desk-post-trade-review` and the Risk Manager, Execution Trader and Trade Reviewer roles. Change its size, price or costs everywhere or nowhere. The SDK snippets in `hyperliquid-orders` use a round size on purpose and are not part of it.
+ **Verify snippets** against the SDK versions named in `docs/PROVENANCE.md`, and update that file when you bump one.

## Layout

+ `agents/<role>.md`: frontmatter (`name`, `title`, `description`, `seat`, `skills`, `writes_to_exchange`), a **Bot profile** section and a **System prompt** section. Exactly one role writes to the exchange.
+ `skills/<name>/SKILL.md`: frontmatter (`name` equal to the directory, `description` under 1024 characters, `license`, `metadata`) and a body under 320 lines.
+ `SETUP.md`: the single runbook a Grok Bot follows. Keep it linear and honest about what Grok Bot can and cannot do.
+ `scripts/`: stdlib-only Python. User tools are read-only and keyless and share the `--schema`, `--json` and error contract in `cli_contract.py`.
+ `hooks/`: the Claude Code exchange-write gate. When unsure, it asks.

## Checks

```bash
bash scripts/check.sh
```

CI runs the same script. It needs no network, key or account, and covers:

+ **Structure:** frontmatter, name and directory agreement, description length, internal links, strict-YAML-safe values and the one-writer rule.
+ **Distribution** (`check_manifests.py`): every plugin manifest agrees on name, version and counts, every declared path exists, and every documented install command, release tag and listing URL resolves to this repository at the current release. `GEMINI.md` must repeat the approval paragraph of `rules/hypergrok-team.mdc` verbatim.
+ **Package and template** (`check_npm_package.py`, `check_grok_template.py`): the npm archive holds exactly the listed files, and `template/grok-bot.json` matches the shipped skill bytes.
+ **Boundary contracts:** fixtures for the exchange-write gate, the standing mandate, release bumps, stop-cost arithmetic and order rounding, each with negative cases. The stop-cost and order-rounding suites also run seeded generated cases: sizing is compared with the same arithmetic in exact fractions, and every published price and size helper is checked against its bound, the venue grid and the SDK wire encoding. A failure prints its seed and case number.
+ **CLI journeys** (`cli_journeys.py`): the tools run end to end against a loopback API, covering schemas, bad input, market and account reads, sizing, dataset collection, replay and tamper refusal.

After a skill edit, also run `validate-agent-skills skills`. The per-tool unit suites run with `python3 -m unittest discover -s scripts -p 'test_*.py'`. For new coverage, prefer a journey or a boundary contract over more unit tests. Never use a live trade to test prose.

## Releasing

`SETUP.md` clones a pinned tag, so the desk a user builds is the desk that was reviewed. Changed instructions reach users only through a release, and these steps land in one commit.

1. **Changelog.** Move `## Unreleased` under a new version heading with today's date.
2. **Skill versions.** Bump `metadata.version` in each skill whose body changed. Leave the others alone.
3. **Pins and hashes.** Preview with `python3 scripts/release_bump.py X.Y.Z --dry-run`, then run it without `--dry-run`. It updates the manifests, the marketplace `source.ref` values, the install pins and the template skill hashes together or not at all, then runs the check. It refuses ambiguous prose versions until you reconcile them by hand, and refuses a version whose tag already exists. Only `CHANGELOG.md` and this file may name older tags.
4. **Tag.** Commit, then `git tag -a vX.Y.Z -m "..." && git push origin main vX.Y.Z`.
5. **GitHub release.** Publish one for the tag. Gemini CLI installs the latest release and, when it has exactly one asset, installs that asset instead of the source, so do not attach the npm archive as the only asset.
6. **npm.** `gh workflow run publish.yml -R galleonlabs/hypergrok-trading-desk` is a dry run; add `-f dry_run=false` to publish. The workflow uses npm trusted publishing and no token. It runs only from `main`, needs green CI and the release tag on the exact commit, verifies the packed archive, publishes it and compares the registry bytes. Run it before `main` moves on. If Actions is unavailable, stage the reviewed archive with `npx -y npm@11.21.1 stage publish <archive> --access public`; the owner approves it with 2FA on npmjs.com.
7. **Grok Bot template.** Update the public template from `template/grok-bot.json` following `docs/GROK_BOT_TEMPLATE.md`, then run `python3 scripts/check_public_template.py --live`. If the share link changes, update it in the README, FAQ and template file, and set it as the repository homepage: `gh repo edit galleonlabs/hypergrok-trading-desk --homepage <link>`.
8. **Prove it.** In an empty directory, run `SETUP.md` section 1 verbatim: clone the tag, then `bash scripts/check.sh`.

Versions number this repository's instruction set, not anything it installs. `v1.1.0` is the first tag: the 1.0.0 changelog entry describes this desk, but the tag of that name pointed at an unrelated earlier project and was removed.

## Credit and reuse

Keep existing authorship and licence notices in contributions and derived work. [ATTRIBUTION.md](ATTRIBUTION.md) explains the MIT notice and offers an optional credit line.
