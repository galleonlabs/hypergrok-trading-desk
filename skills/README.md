# Skills

Sixteen skills in the portable `SKILL.md` format (`name` and `description` frontmatter, body under 300 lines). Grok Bot reads them from `/workspace/hypergrok/skills/<name>/SKILL.md` and saves each as a shared skill; Grok Build, Cursor and Claude Code load the directory as a plugin.

## Hyperliquid skills (how to work with the exchange)

| Skill | What it teaches | Writes to the exchange? | Primary users |
| --- | --- | --- | --- |
| [hyperliquid-setup](hyperliquid-setup/SKILL.md) | Networks, SDK install, connectivity check, API wallet provisioning through the secure secret store, readiness check, rotation | no | Desk Lead, Execution Trader |
| [hyperliquid-market-data](hyperliquid-market-data/SKILL.md) | Prices, book depth, funding, OI, volume, candles, spot and HIP-3 metadata, dataset saving | no | Market Analyst, Strategist, Risk Manager |
| [hyperliquid-account](hyperliquid-account/SKILL.md) | Positions, margin, balances, open orders with trigger detail, fills, funding paid, ledger, order status, portfolio, fees, rate limit | no | Risk Manager, Execution Trader, Trade Reviewer |
| [hyperliquid-orders](hyperliquid-orders/SKILL.md) | Limit, IOC, TP/SL with grouping, cloid, cancel, modify, rounding, response statuses, error strings | **yes** | Execution Trader |
| [hyperliquid-positions](hyperliquid-positions/SKILL.md) | Leverage and margin mode, isolated margin, margin tiers, liquidation, closing, protection checks | **yes** (leverage, closes) | Execution Trader, Risk Manager (reads) |
| [hyperliquid-websocket](hyperliquid-websocket/SKILL.md) | Live subscriptions, watch processes, heartbeat and supervision | no | Market Analyst, Execution Trader, Strategist |
| [hyperliquid-advanced](hyperliquid-advanced/SKILL.md) | Dead-man's switch, TWAP, spot orders, expiresAfter, agent approval from code, sub-accounts, HIP-3, what the desk does not do | **yes** (some) | Execution Trader |
| [hyperliquid-api-reference](hyperliquid-api-reference/SKILL.md) | Every request type and action, statuses, asset ids, tick/lot, rate limits, WS types, error strings, docs links | no | everyone |

## Desk skills (how the team works)

| Skill | What it covers | Primary users |
| --- | --- | --- |
| [desk-operating-model](desk-operating-model/SKILL.md) | Roles, seats, workspace, engagement levels, evidence standard, approval model, handoff format | everyone |
| [desk-trade-lifecycle](desk-trade-lifecycle/SKILL.md) | The seven stages of a trade, the proposal file, the ticket, approval by id, definitions of done | Desk Lead, everyone |
| [desk-risk-limits](desk-risk-limits/SKILL.md) | The limits file, sizing arithmetic, margin tiers, book check, veto rules | Risk Manager |
| [desk-execution-protocol](desk-execution-protocol/SKILL.md) | Pre-send checklist, single send, unknown results, reconciliation, rehearsal rule | Execution Trader |
| [desk-monitoring](desk-monitoring/SKILL.md) | Routines, the desk brief, watches and alert conditions | Desk Lead, Risk Manager, Market Analyst |
| [desk-post-trade-review](desk-post-trade-review/SKILL.md) | Journal format, trade review, weekly review, incident review | Trade Reviewer |
| [desk-incident-response](desk-incident-response/SKILL.md) | Playbooks for unknown sends, mismatches, unprotected positions, outages, key compromise | Execution Trader, Risk Manager, Desk Lead |
| [desk-strategy-lab](desk-strategy-lab/SKILL.md) | Rules first, honest backtests, sanity checks, paper trading on testnet | Strategist |

## Conventions

- Frontmatter: `name` (matches the directory), `description` (what and when, under 1024 characters), `license`, `metadata` (`version`, `author`, `category`, and `network-default` for Hyperliquid skills).
- Bodies: purpose, concepts, copy-pasteable commands (`curl` for reads, Python SDK first and TypeScript second for writes), procedure, pitfalls. No strategy content, no return claims, no emoji.
- Every write path says who may use it and under what approval. Every read says which address to use.
- Snippets are self-contained: each includes its own environment and key loading so a Bot can copy one block and run it.
