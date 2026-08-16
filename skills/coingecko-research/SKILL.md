---
name: coingecko-research
description: Research tokens and markets through CoinGecko.
version: 0.1.0
author: Galleon Labs, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HyperGrok, Hyperliquid, Trading]
    related_skills: []
---

# Coingecko Research

Use for token identity, supply, price, market cap and venue context. HyperGrok analyses autonomously but never treats analysis as approval to move funds.

## When to use

Use for token identity, supply, price, market cap and venue context.

## Procedure

1. Resolve the canonical CoinGecko ID rather than guessing a ticker.
2. Set the known tier and optional key.
3. Run `hypergrok coingecko <coin-id>`.
4. Cite returned timestamps and flag tier limits.

## Pitfalls

Tickers collide. Never answer mutable market data from memory when the live call failed.

## Verification

Return the live source, observation time, facts, inference, failed gates and one next owner. For execution, include the plan hash, cloid, builder attribution and verified effect.
