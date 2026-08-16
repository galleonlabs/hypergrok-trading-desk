---
name: hyperliquid-intelligence
description: Read Hyperliquid markets and accounts with live data.
version: 0.1.0
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

1. Run `hypergrok market <COIN>` and record the source and observation time.
2. Run `hypergrok account <ADDRESS>` when account context is authorised.
3. Separate returned facts from interpretation.
4. Hand exact evidence to the desk lead.

## Pitfalls

An L2 or account response is a snapshot. Never infer strategy intent from positions or PnL.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid, builder attribution and verified effect.
