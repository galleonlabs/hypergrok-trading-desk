---
name: desk-setup
description: Validate a HyperGrok desk before research or trading.
version: 0.1.0
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

1. Run `hypergrok health`.
2. Confirm testnet, caps and the disclosed builder address.
3. Run `hypergrok market BTC` as a live read smoke test.
4. Do not request or store a seed phrase.

## Pitfalls

A green read check is not live-trading readiness. Main-wallet builder approval is separate and must remain visible.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid, builder attribution and verified effect.
