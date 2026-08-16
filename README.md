# HyperGrok

**Turn your Grok Bot into a Hyperliquid trading desk.**

HyperGrok is not a bot and not a CLI. It is a set of instructions, role definitions and skills that your own [Grok Bot](https://x.ai/bot) reads and uses to build a small trading desk inside your workspace: seven specialist Bots, one group chat, a shared set of Hyperliquid skills, and a way of working that keeps research, risk, execution and review separate.

You bring the ideas. The desk brings method, live data, sizing arithmetic, careful execution and honest review. It ships no strategies, makes no return claims, and never sends an order you have not approved by ticket id.

## Start

Open Grok Bot and paste this to any Bot:

> Set up the HyperGrok trading desk from https://github.com/galleonlabs/hypergrok-trading-desk/blob/main/SETUP.md. Follow that file from top to bottom, create the seven Bots and the Trading Floor group chat, install the skills, and finish with the receipt it asks for. Do not request any keys or place any orders.

If the Bot cannot open the link from its computer, attach this repository's archive to the conversation and tell it to unpack it to `/workspace/hypergrok`.

Fifteen minutes later you have a desk. It starts as a research desk (no key). When you want to trade with play money, tell the Desk Lead to set up a testnet API wallet.

## The desk

| Bot | Job | Sends to the exchange? |
| --- | --- | --- |
| **Desk Lead** | Your main contact. Routes work, runs the trade lifecycle, keeps the desk record. | No |
| **Market Analyst** | Live Hyperliquid data: price, book depth, funding, open interest, volume, candles. Timestamped, sourced briefs. | No |
| **Research Analyst** | Fundamentals, news, catalysts, onchain and social context. Sceptical by design. | No |
| **Strategist** | Helps you turn *your* ideas into explicit rules, backtests them honestly, paper-trades them on testnet. | No |
| **Risk Manager** | Owns your written risk limits, sizes every trade from live account state, watches the book, can refuse. | No |
| **Execution Trader** | The only Bot that places, modifies or cancels orders. One approved ticket, one send, reconciled from the exchange record. | **Yes** |
| **Trade Reviewer** | Keeps the desk journal, reviews process separately from outcome, runs incident reviews. Off the floor. | No |

Six sit in one Grok Bot group chat, the **Trading Floor**. The Trade Reviewer works by direct message. Every trade follows the same path:

```
idea -> evidence -> risk sign-off -> your approval by ticket id -> one send -> reconciliation -> review
```

## What is in the repository

```
SETUP.md            the file your Grok Bot follows to build the desk
agents/             seven roles: a Bot profile card (Name, Job, Description) plus a full system prompt each
skills/             sixteen skills, in the portable SKILL.md format
  hyperliquid-*     how to work with Hyperliquid from the desk computer: setup and API wallets, market data,
                    account state, orders, positions and margin, WebSocket, advanced actions, and a compact API reference
  desk-*            how the desk works: operating model, trade lifecycle, risk limits and sizing, execution protocol,
                    monitoring, post-trade review, incident response, strategy lab
docs/               architecture, FAQ, provenance
rules/, .grok-plugin/, .cursor-plugin/   lets Grok Build and Cursor load the same roles and skills as a plugin
```

Skills are written to the [Agent Skills](https://agentskills.io) convention (`SKILL.md` with `name` and `description` frontmatter), so they also work in Grok Build, Cursor, Claude Code and Hermes. Grok Bot is the primary target: it reads the files from its computer and saves them as skills.

## Hyperliquid skills

Each `hyperliquid-*` skill teaches a Bot to do one family of things against the real API, with copy-pasteable `curl` for reads and Python (official `hyperliquid-python-sdk`) or TypeScript (`@nktkas/hyperliquid`) for everything that signs. Testnet and mainnet, perps and spot, orders with take-profit and stop-loss grouping, client order ids, cancels and modifies, leverage and margin modes, dead-man's switch, TWAP, WebSocket subscriptions, rate limits, tick and lot rules, error strings and what they mean.

What the skills deliberately do not cover: deposits, withdrawals, bridging, transfers between accounts, sub-accounts or vaults, sending USDC or spot tokens, staking, builder fee approvals. You do those in the Hyperliquid app.

## Safety model

- **You approve every send**, by writing the ticket id in chat after seeing the exact ticket. "Yes" is not approval.
- **Only an API wallet key** ever reaches the desk computer, provisioned through Grok Bot's secure secret store, never pasted in chat. API wallets can trade but cannot withdraw. Never a seed phrase, never a main-wallet key.
- **All your Bots share one computer**, so Bot names are not a security boundary; that is why the key is trade-only, and why the desk starts on testnet.
- **Testnet first.** Every new kind of action is rehearsed with play money.
- **No unattended sending.** Routines may read and alert; only a ticket you approved gets sent.
- **No strategies, no return claims.** The Strategist tests your ideas; nothing here tells you what to trade.

Perpetual futures can liquidate an account. This is software and documentation, not financial advice.

## Documentation

| Document | Contents |
| --- | --- |
| [SETUP.md](SETUP.md) | The single entry point for your Grok Bot |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Desk topology, trust boundaries, exclusions |
| [docs/FAQ.md](docs/FAQ.md) | Common questions |
| [docs/PROVENANCE.md](docs/PROVENANCE.md) | Sources studied and how they were used |
| [skills/README.md](skills/README.md) | Skill index and which Bot uses which |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to improve the desk |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

MIT licensed. Built by [Galleon Labs](https://github.com/galleonlabs).
