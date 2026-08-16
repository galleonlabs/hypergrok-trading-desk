---
name: hyperliquid-intelligence
description: Read Hyperliquid markets and accounts with live data.
version: 1.0.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Hyperliquid Intelligence

Use for market structure, funding, positions, orders and margin. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use for market structure, funding, positions, orders and margin.

## Procedure

1. Select `HYPERGROK_NETWORK=testnet` or `mainnet`; mainnet also requires `HYPERGROK_ENABLE_MAINNET=I_UNDERSTAND`.
2. Run `hypergrok market <COIN>` and record network, source and observation time.
3. Run `hypergrok account <ADDRESS>` when account context is authorised.
4. Separate returned facts from interpretation.
5. Hand exact evidence to the desk lead.

## Pitfalls

An L2 or account response is a snapshot. Never infer strategy intent from positions or PnL.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner.
