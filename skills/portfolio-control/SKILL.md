---
name: portfolio-control
description: Review whole-book exposure and position protection.
version: 0.1.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Portfolio Control

Use before adding risk and during position monitoring. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use before adding risk and during position monitoring.

## Procedure

1. Run `hypergrok account <ADDRESS>`.
2. Reconcile positions, open orders, margin and concentration.
3. Check whether stops or reduce-only exits actually exist.
4. Distinguish stale data, unprotected risk and intentional exposure.

## Pitfalls

A plan file is not an open order. PnL does not prove the thesis or the quality of the decision.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid, builder attribution and verified effect.
