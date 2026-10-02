---
name: hypergrok-bootstrap
description: Build and verify a HyperGrok trading desk from the pinned public release. Use for first-run setup, repair, or a readiness check. Starts with a live zero-key Opening Bell, installs the seven role profiles and seventeen shared skills, prepares the Trading Floor, and returns an evidence receipt. Read-only by default; never requests a wallet or places an order.
license: MIT
metadata:
  version: "1.0.8"
  author: Galleon Labs
  category: desk
---

# HyperGrok bootstrap

Turn a fresh shared **Desk Lead** into a working HyperGrok desk. Finish with evidence, not a claim that setup probably worked.

## Safety boundary

Bootstrap is research-only.

- Do not request, read or store an API wallet, private key, seed phrase or exchange secret.
- Do not call Hyperliquid `/exchange`, create an order, alter leverage, move funds or change approval settings.
- Public `POST /info` market reads are allowed. State mainnet, request type and UTC time.
- Local writes are limited to `/workspace/hypergrok` and `/workspace/trading-desk`.
- Creating the seven named Bots and the private Trading Floor group is in scope. Sharing anything publicly is not.
- If a capability is unavailable, return the exact manual card or step and continue independent supported work. Do not pretend it happened or mark dependent checks complete.

## 1. Install the reviewed release

If `/workspace/hypergrok` is already a Git checkout, preserve it. Verify that `plugin.json` names version `1.4.8` and that `git rev-parse HEAD` equals `git rev-parse 'refs/tags/v1.4.8^{commit}'`. If the tag is unavailable, the version differs or the commits differ, report the existing version and commit and stop dependent setup. A structural pass from an older release is insufficient; do not overwrite, reset or switch the existing checkout. Otherwise clone the pinned release:

```bash
mkdir -p /workspace && cd /workspace
git clone --depth 1 --branch v1.4.8 https://github.com/galleonlabs/hypergrok-trading-desk.git hypergrok
cd /workspace/hypergrok
git rev-parse HEAD
bash scripts/check.sh
```

Run `bash scripts/check.sh` for an existing verified checkout too. Stop if the structural check fails. Record the printed commit for `desk.md`.

Before using any agent profile or System prompt, run `git diff --exit-code refs/tags/v1.4.8 -- agents/` in the checkout. A matching HEAD can still have changed working files. If the command reports differences or fails, name the affected paths, stop dependent setup and preserve the user's files.

## 2. Ring the Opening Bell

Show useful output before asking the user configuration questions:

```bash
cd /workspace/hypergrok
python3 scripts/opening_bell.py --coin ETH
```

Return the output verbatim. It is a public market snapshot, not a signal. If it is unavailable, say so and continue offline; do not substitute stale or invented figures.

## 3. Prepare the desk

```bash
mkdir -p /workspace/trading-desk/{proposals,briefs,research,strategies,data,journal/incidents,watch}
cd /workspace/hypergrok
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
```

A warning for the not-yet-written desk record is expected at this stage. A failed repository or public API check is not.

## 4. Build the team

Read `docs/ARCHITECTURE.md`, `skills/desk-operating-model/SKILL.md`, every file in `agents/`, and `skills/README.md`.

Select one Bot for each agent file's **Bot profile** and **System prompt**:

1. Desk Lead
2. Market Analyst
3. Research Analyst
4. Strategist
5. Risk Manager
6. Execution Trader
7. Trade Reviewer

Read back each selected Bot's native Name, Job and Description and compare them exactly with its profile. Verify its standing instructions before reuse or after creation. Supported evidence is either the complete stored System prompt matching this release, or readback of the exact full System prompt delivered as a standing-instructions message, the Bot's acknowledgement that it follows the pinned agent file, and that local file's hash matching the expected bytes from `git show 'refs/tags/v1.4.8:agents/<name>.md'`. Hashing an arbitrary local file is insufficient. Label the latter `delivery-and-pointer`: it proves delivery and the checked target, not byte-identical persistent memory. A matching Description, summary or path alone is insufficient.

If an existing seat differs, is missing or cannot be verified, create a separate Bot from the reviewed profile and deliver its full System prompt as directed in `SETUP.md` section 4. Preserve existing Bots; reuse does not authorize replacing an unrelated desk's profiles, prompts or memories. If a needed creation step or both supported verification methods are unavailable, return that role's exact manual card and keep its readiness pending. Record the native Bot ID for every verified seat.

Use `assets/mascot.jpg` as the avatar when the product supports it. Do not add unrelated memories, conversation history or private files. Continue independent local preparation and checks; keep Bot and group readiness pending until existence and instruction evidence are verified.

Inspect the shared skills before installing anything. A Desk Lead added from the public HyperGrok template carries the reviewed bootstrap skill. Install the remaining release skills from this checkout; do not create duplicate skills. Verify actual native shared instructions before recording them as `template`; that status means an already-present verified skill, not that all seventeen were exported by the public share.

Use supported native readback to inspect each skill's exact name, exact description and complete Markdown body. Grok stores name and description separately and rebuilds the YAML wrapper without source license or metadata. Compare the body after removing frontmatter, leading empty lines and the final newline only; preserve all internal text, spaces and line breaks. Do not strip trailing spaces from internal lines or summarize content. Full-file hashes in `template/grok-bot.json` continue to verify all seventeen pinned checkout files. Checkout hashes and staged save arguments do not prove what the native product stored.

For each of the seventeen `skills/*/SKILL.md` files:

1. If an existing shared skill passes complete native readback against the release, enable it for the selected Desk Lead and record `template`.
2. If it is missing, save the exact source name, description and normalized body. Read the saved skill back and record `installed` only when they match.
3. If the product cannot save that body, save the source name and description with a pointer instructing it to read `/workspace/hypergrok/skills/<name>/SKILL.md`. Verify the complete stored pointer text and the target's full-file hash, then record `pointer`.
4. If native content differs or cannot be read back completely, record `mismatch` with the reason and fail native readiness. Do not overwrite shared content used by unrelated desks without user authority. Never claim a skill is current from its name, a local file or a staged argument alone.

The final receipt must contain exactly seventeen unique skill names and one status for each: `template`, `installed`, `pointer` or `mismatch`.

Create a private group chat named **Trading Floor** with the six selected Bot IDs for Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager and Execution Trader. Reuse an existing group only when its ID is already selected for this desk and native readback confirms exactly those member IDs; its name alone is insufficient. Preserve prior groups. Record the Floor ID and member IDs and direct the welcome message and role checks to that group. The Trade Reviewer stays in DM. If group creation or membership readback is unavailable, return the exact manual step and keep group readiness pending.

## 5. Record the research desk

Unless the user explicitly selects testnet or mainnet, create a research desk:

Record only verified Bot, group and member IDs as observed; mark uncreated, manual or unverified roles and any unverified Floor as pending rather than copying the desired seven-Bot/six-member inventory as an observed result.

```markdown
# Desk record

- created: <UTC time>
- instructions commit: <commit from step 1>
- engagement level: research
- network: mainnet-for-reads
- account: none
- bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer
- bot ids: <role to verified native Bot ID>
- group chats: Trading Floor (6)
- trading floor id and member ids: <verified native group and six Bot IDs>
- risk limits: not yet written
- standing approvals: none
- exchange approval gate: unverified; required before provisioning any API wallet key
- unprotected position deadline: not applicable
- status: research-only; no API wallet provisioned
```

Write it to `/workspace/trading-desk/desk.md`. Do not provision trading access during bootstrap.

## 6. Verify and return the receipt

Run:

```bash
cd /workspace/hypergrok
python3 scripts/desk_doctor.py --desk-root /workspace/trading-desk
python3 scripts/opening_bell.py --coin BTC
```

Then perform the five role checks in `SETUP.md` section 9. Return:

- release tag and installed commit
- Opening Bell source, network and UTC time
- each Bot: ID, created/reused/manual card, profile comparison and standing-instruction proof method; state any `delivery-and-pointer` limitation
- each skill: status, native readback method or unavailable reason, normalized stored body length/hash and comparison result; identify pointer evidence separately
- Trading Floor ID and verified member IDs
- desk doctor results, including warnings
- five role-check results
- exact local paths for `desk.md` and the repository
- this sentence: **Setup stayed read-only: no key requested and no order created or sent.**

Do not say the desk is ready if a required check failed. Name the failed check and the next safe action.
