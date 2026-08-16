---
name: pretrade-risk
description: Size trades and reject plans that breach desk limits.
version: 1.0.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Pretrade Risk

Use for every proposed order before an execution plan exists. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use for every proposed order before an execution plan exists.

## Procedure

1. Read live equity and existing book exposure.
2. Require entry, stop and risk percentage.
3. Run `hypergrok size --equity ... --entry ... --stop ... --risk-pct ... --max-notional ...`.
4. Stress price gap and correlated exposure.
5. Approve the size or refuse with one exact failed gate.

## Pitfalls

Do not size from desired profit. Cross-margin exposure means position-level margin is not the whole risk.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner.
