---
name: posttrade-review
description: Review trade process separately from realised outcome.
version: 1.0.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Posttrade Review

Use after fills, exits, execution errors or a desk review. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use after fills, exits, execution errors or a desk review.

## Procedure

1. Reconstruct plan, approval, order response and fills by cloid.
2. Compare planned and filled price, size, fee and timing.
3. Judge thesis, risk and execution separately.
4. Identify one repeatable leak or one control that worked.
5. Return a compact desk brief with evidence and next owner.

## Pitfalls

A winning trade can be a bad decision. Counterfactual hold returns are not proof the exit was wrong.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid and verified effect.
