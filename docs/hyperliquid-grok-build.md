# Hyperliquid trading desk for Grok Build

HyperGrok turns Grok Build into a Hyperliquid trading desk: seven roles and seventeen agent skills for research, risk, execution and review. It is open source (MIT), starts read-only on public market data, and sends an order only for a ticket you approve by id with a trade-only API wallet you provide. For the full seven-Bot desk with a shared Trading Floor, use [Grok Bot](../README.md#start).

## Install

Install the reviewed release:

```bash
grok plugin install galleonlabs/hypergrok-trading-desk@v1.7.0 --trust
```

Or register this repository's Grok marketplace, which pins the same release, and install by name:

```bash
grok plugin marketplace add galleonlabs/hypergrok-trading-desk
grok plugin install hypergrok --trust
```

The installation format was tested with Grok Build 1.0.30 for an earlier release; 1.6.1 has no native installation proof yet.

Load `desk-operating-model` before asking the roles to act.

## What loads

+ **Seventeen skills**: Hyperliquid setup, market data, account state, orders, positions, WebSocket feeds, advanced actions and a compact API reference, plus the desk skills for the trade lifecycle, risk limits and sizing, execution, monitoring, post-trade review, incidents and the strategy lab.
+ **Seven roles** from `agents/`: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader and Trade Reviewer. Only the Execution Trader writes to the exchange.

Grok Build reports the skills and agents directories; loading `rules/` is not verified, and the Claude Code exchange-write hook does not load. The desk's approval by ticket id still applies.

## First run, no keys

Ask for a market brief, for example "Brief me on ETH". The reads use Hyperliquid's public `/info` endpoint, with no wallet or account. The same read works from a terminal:

```bash
git clone --depth 1 --branch v1.7.0 https://github.com/galleonlabs/hypergrok-trading-desk.git
cd hypergrok-trading-desk
python3 scripts/opening_bell.py --coin ETH
```

## When you want to trade

Add a testnet API wallet first and practise with play money; add a mainnet one when you decide to trade real money. The desk only ever asks for a trade-only API wallet key from your own secret store, never a seed phrase or main-wallet key; see [`hyperliquid-setup`](../skills/hyperliquid-setup/SKILL.md). Every trade goes idea, evidence, Risk Manager sign-off, your approval by ticket id, one send, reconciliation, review.

Perpetual futures can liquidate an account. HyperGrok is documentation and instructions, not financial advice.

## More

+ [README](../README.md) and [how the desk works](ARCHITECTURE.md)
+ Other tools: [Claude Code](hyperliquid-claude-code.md) · [Cursor](hyperliquid-cursor.md) · [Codex](hyperliquid-codex.md) · [Gemini CLI](hyperliquid-gemini-cli.md) · Grok Bot: [README Start](../README.md#start)
+ [FAQ](FAQ.md)
