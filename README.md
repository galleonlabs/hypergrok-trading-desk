<p align="center">
  <img src="assets/mascot-320.jpg" width="160" alt="HyperGrok mascot">
</p>

<h1 align="center">HyperGrok</h1>

<p align="center"><strong>Turn your Grok Bot into a 7-agent Hyperliquid trading desk.</strong></p>

Paste this repo into [Grok Bot](https://x.ai/bot). Fifteen minutes later you have research, risk, execution and review as separate agents, sitting in one group chat, wired straight into Hyperliquid. They brief you on markets, size your trades, send what you approve and tell you honestly how it went. You bring the ideas. Let them cook.

## Start

Open Grok Bot and paste this to any Bot:

> Set up the HyperGrok trading desk from https://github.com/galleonlabs/hypergrok-trading-desk/blob/main/SETUP.md. Follow that file from top to bottom, create the seven Bots and the Trading Floor group chat, install the skills, and finish with the receipt it asks for.

That is the whole install. The desk starts in research mode; add a testnet API wallet when you want to practise with play money, and a mainnet one when you are ready.

## Meet the desk

| Bot | What they do for you |
| --- | --- |
| **Desk Lead** | Your main contact. Runs the floor, keeps every trade moving through the same clean process. |
| **Market Analyst** | Live Hyperliquid data on demand: price, depth, funding, open interest, candles. Timestamped and sourced. |
| **Research Analyst** | What is happening and what is scheduled: fundamentals, news, catalysts, onchain and social context. |
| **Strategist** | Turns your idea into explicit rules, backtests it honestly on Hyperliquid history, paper-trades it on testnet. |
| **Risk Manager** | Keeps your written limits, sizes every trade from your live account, watches the book, and can say no. |
| **Execution Trader** | The one Bot with the keys. Sends the ticket you approved, once, and reconciles it from the exchange record. |
| **Trade Reviewer** | Keeps the desk journal and grades every trade on process and outcome, separately. |

Six sit together on the **Trading Floor** group chat; the Trade Reviewer works by DM. Every trade follows the same path:

```
idea -> evidence -> risk sign-off -> your approval -> one send -> reconciliation -> review
```

## A day on the desk

**"Brief me on ETH."** The Market Analyst pulls mid, mark, funding, open interest, 24h volume and depth at 5/10/25 bps from the exchange, and posts a brief with sources and UTC times.

**"I want to long ETH at 3,000 with a stop at 2,900."** The Desk Lead opens `HG-20260816-01`, the Risk Manager reads your account live and comes back with a ticket:

```
TICKET HG-20260816-01 | mainnet
market ETH-PERP  side buy  size 0.51 ETH (~$1,530)
entry limit 3,000 Gtc     stop sell 0.51 @ 2,900 market (sent with the entry)
risk $51.00 = 0.5% of equity $10,200 (clearinghouseState 14:11 UTC)  R = 100
approve with: "approve HG-20260816-01"
```

You type `approve HG-20260816-01`. The Execution Trader sends entry and stop in one grouped order, reads the exchange response, confirms it on the book, and reports the order ids. When it closes, the Trade Reviewer tells you what it cost, whether the process was clean, and one thing worth keeping.

**"Can we test whether funding extremes mean-revert?"** The Strategist writes the rules with you, pulls a year of candles and funding, runs a backtest with fees and an out-of-sample split, and reports the trade distribution, not a headline number. Like it? Paper-trade it on testnet through the same desk.

**"Watch ETH and tell me if funding flips negative."** A routine on the desk computer watches the WebSocket feed and pings you when it happens.

## What the desk knows

Sixteen skills, in the portable `SKILL.md` format, shared by all your Bots.

**Hyperliquid** - setup and API wallets, market data, account state, orders (limit, IOC, take-profit and stop-loss with grouping, client order ids), positions and margin, WebSocket feeds, advanced actions (dead-man's switch, TWAP, spot), and a compact API reference. Copy-pasteable `curl` for reads; the official Python SDK and `@nktkas/hyperliquid` for anything that signs.

**Desk** - how the team works: operating model, the trade lifecycle and ticket, risk limits and sizing arithmetic, the execution protocol, monitoring and routines, post-trade review, incident playbooks, and the strategy lab.

## Built for real money

- **You approve every trade**, by ticket id, after seeing the exact order.
- **A trade-only API wallet**, provisioned through Grok Bot's secure secret store, is the only key the desk ever holds. It can trade; it cannot withdraw.
- **Testnet first.** Every new kind of action is rehearsed with play money before it touches mainnet.
- **One writer.** Six Bots read; one Bot sends, once per approval, and reconciles by client order id.
- **A reviewer who keeps you honest.** Process and outcome graded separately, in a journal you can read.

Perpetual futures can liquidate an account. HyperGrok is documentation and instructions, not financial advice.

## Also runs in Grok Build, Cursor and Claude Code

The same `agents/`, `skills/` and `rules/` load as a plugin. Open the repository, enable it, run `/desk-operating-model`.

## Inside the repository

```
SETUP.md     what your Grok Bot follows to build the desk
agents/      seven roles: Bot profile card + full system prompt
skills/      sixteen skills (hyperliquid-*, desk-*)
docs/        how it works, FAQ, provenance
assets/      the mascot - use it as your Bots' avatar
```

| Doc | |
| --- | --- |
| [How the desk works](docs/ARCHITECTURE.md) | roles, files, trust boundaries |
| [FAQ](docs/FAQ.md) | keys, approvals, testnet, customising the team |
| [Skills index](skills/README.md) | every skill and who uses it |
| [Provenance](docs/PROVENANCE.md) | sources and licences |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) | |

MIT. Built by [Galleon Labs](https://github.com/galleonlabs).
