---
name: incident-response
description: Contain Hyperliquid trading incidents without widening harm.
version: 1.0.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Incident Response

Use for suspected key compromise, duplicate orders, stale plans or broken protection. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use for suspected key compromise, duplicate orders, stale plans or broken protection.

## Procedure

1. Stop new execution and preserve the cloid and receipts.
2. Read live account and order state.
3. Reconcile unknown submissions before cancellation decisions.
4. Prepare cancel-all or dead-man-switch action for explicit user approval.
5. Rotate the affected API wallet outside this plugin.

## Pitfalls

HyperGrok provides guidance, not an automated cancel-all command. Never withdraw or transfer funds as an improvised incident response.

## Verification

Return the incident scope, cloid reconciliation, known effects, unknowns, containment and one next owner.
