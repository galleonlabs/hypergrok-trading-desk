---
name: order-execution
description: Plan and execute one disclosed Hyperliquid order safely.
version: 0.1.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Order Execution

Use only after thesis and risk sign-off. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use only after thesis and risk sign-off.

## Procedure

1. Create a plan with `hypergrok plan-order` and preserve its SHA-256.
2. Show account, network, side, size, limit, expiry, cloid and 1 bp builder fee.
3. Obtain the user’s exact approval of that plan.
4. Execute once with the exact hash and `--execute`.
5. Verify the response or reconcile the cloid before any further action.

## Pitfalls

A timeout is an unknown result, not a failed order. Never retry a send merely because the response was lost.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid, builder attribution and verified effect.
