---
name: desk-setup
description: Validate both networks before research or trading.
version: 1.0.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Desk Setup

Use for installation, configuration, upgrades and readiness checks. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use for installation, configuration, upgrades and readiness checks.

## Procedure

1. Run `hypergrok doctor` and `hypergrok market BTC` against testnet.
2. Repeat both read-only checks with `HYPERGROK_NETWORK=mainnet HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND`.
3. Confirm caps, builder address, builder balance and account-abstraction mode.
4. When a trading account is supplied, run `hypergrok doctor --user <ADDRESS>` to verify its 1 bp approval.
5. Do not request or store a seed phrase or main-wallet key.

## Pitfalls

A green read check is not execution readiness. Main-wallet builder approval is separate, revocable and never automated.

## Verification

Return both network receipts, the actual seven-role runtime mode, failed readiness gates and one next action.
