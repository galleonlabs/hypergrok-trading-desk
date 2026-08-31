---
name: hypergrok-bootstrap
description: Build and verify a HyperGrok trading desk from the pinned public release. Use for first-run setup, repair, or a readiness check. Starts with a live zero-key Opening Bell, installs the seven role profiles and seventeen shared skills, prepares the Trading Floor, and returns an evidence receipt. Read-only by default; never requests a wallet or places an order.
license: MIT
metadata:
  version: "1.0.0"
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
- If a capability is unavailable, return the exact manual card or step. Do not pretend it happened.

## 1. Install the reviewed release

If `/workspace/hypergrok` is already a Git checkout, read its `plugin.json` and run its check; do not overwrite it. Otherwise:

```bash
mkdir -p /workspace && cd /workspace
git clone --depth 1 --branch v1.3.0 https://github.com/galleonlabs/hypergrok-trading-desk.git hypergrok
cd /workspace/hypergrok
git rev-parse HEAD
bash scripts/check.sh
```

Stop if the structural check fails. Record the printed commit for `desk.md`.

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

Create one Bot from each agent file's **Bot profile** and **System prompt**:

1. Desk Lead
2. Market Analyst
3. Research Analyst
4. Strategist
5. Risk Manager
6. Execution Trader
7. Trade Reviewer

Use `assets/mascot.jpg` as the avatar when the product supports it. Do not add unrelated memories, conversation history or private files. If Bot creation is unavailable, return seven clearly labelled copy-and-paste cards and wait for the user to create them.

Install all seventeen `skills/*/SKILL.md` files as shared skills without changing their text. If a full skill is too long for the product, install a pointer skill that reads that exact file at use time. Report full versus pointer installs.

Create a private group chat named **Trading Floor** with Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager and Execution Trader. The Trade Reviewer stays in DM. Post the floor welcome message from `SETUP.md` section 6.

## 5. Record the research desk

Unless the user explicitly selects testnet or mainnet, create a research desk:

```markdown
# Desk record

- created: <UTC time>
- instructions commit: <commit from step 1>
- engagement level: research
- network: mainnet-for-reads
- account: none
- bots: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer
- group chats: Trading Floor (6)
- risk limits: not yet written
- standing approvals: none
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
- each Bot: created or manual card returned
- each skill: full or pointer install
- Trading Floor membership
- desk doctor results, including warnings
- five role-check results
- exact local paths for `desk.md` and the repository
- this sentence: **Setup stayed read-only: no key requested and no order created or sent.**

Do not say the desk is ready if a required check failed. Name the failed check and the next safe action.
