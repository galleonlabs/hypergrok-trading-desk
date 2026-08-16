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

1. Read the market's real constraints with `hypergrok limits <COIN> --equity <EQUITY>`. Record max leverage, the margin tier that applies at the intended notional, size decimals and the minimum order value.
2. Read live equity and existing book exposure.
3. Require entry, stop and risk percentage.
4. Run `hypergrok size --equity ... --entry ... --stop ... --risk-pct ... --max-notional ...`.
5. Stress price gap and correlated exposure against the tier that applies, not the headline leverage.
6. Approve the size or refuse with one exact failed gate.

## Pitfalls

Do not size from desired profit. Cross-margin exposure means position-level margin is not the whole risk.

HyperGrok imposes no risk-per-trade ceiling of its own. The absence of a refusal from `hypergrok size` is not risk approval; it means the tool had no opinion. Sizing judgment is this role's responsibility, taken from the exchange limits and the book, not from a default in a config file.

Headline max leverage is the top tier only. A larger position silently falls into a lower-leverage tier, so a size that looks financeable at 40x may not be at the notional actually intended.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner.
