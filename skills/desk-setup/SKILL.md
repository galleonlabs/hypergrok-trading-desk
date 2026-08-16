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
3. Confirm caps and every live execution-readiness gate.
4. When a trading account is supplied, run `hypergrok doctor --user <ADDRESS>` to verify its account-specific readiness.
5. Do not request or store a seed phrase or main-wallet key.

## Pitfalls

A green read check is not execution readiness. Do not infer an account-specific gate from general endpoint health.

## Verification

Return both network receipts, the actual seven-role runtime mode, failed readiness gates and one next action.
