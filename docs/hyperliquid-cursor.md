# Hyperliquid trading desk for Cursor

HyperGrok turns Cursor into a Hyperliquid trading desk: seven roles and seventeen agent skills for research, risk, execution and review. It is open source (MIT), starts read-only on public market data, and sends an order only for a ticket you approve by id with a trade-only API wallet you provide.

## Install

Clone the reviewed release:

```bash
git clone --depth 1 --branch v1.7.0 https://github.com/galleonlabs/hypergrok-trading-desk.git
```

Open the `hypergrok-trading-desk` folder in Cursor and enable the plugin; `.cursor-plugin/plugin.json` points it at the skills, roles and rules. The plugin is also listed on [cursor.directory](https://cursor.directory/plugins/hypergrok).

Load `desk-operating-model` before asking the roles to act.

## What loads

+ **Seventeen skills**: Hyperliquid setup, market data, account state, orders, positions, WebSocket feeds, advanced actions and a compact API reference, plus the desk skills for the trade lifecycle, risk limits and sizing, execution, monitoring, post-trade review, incidents and the strategy lab.
+ **Seven roles** from `agents/`: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader and Trade Reviewer. Only the Execution Trader writes to the exchange.
+ **The desk rule** from `rules/hypergrok-team.mdc`: analysis is not approval, and a send needs your exact approval phrase with the ticket id.

The Claude Code exchange-write hook does not load in Cursor.

## First run, no keys

From the checkout, in Cursor's terminal:

```bash
python3 scripts/opening_bell.py --coin ETH
```

It reads Hyperliquid's public `/info` endpoint only: price, funding, open interest, spread and depth, with no wallet or account. Ask the agent for a market brief next, for example "Brief me on ETH".

## When you want to trade

Add a testnet API wallet first and practise with play money; add a mainnet one when you decide to trade real money. The desk only ever asks for a trade-only API wallet key from your own secret store, never a seed phrase or main-wallet key; see [`hyperliquid-setup`](../skills/hyperliquid-setup/SKILL.md). Every trade goes idea, evidence, Risk Manager sign-off, your approval by ticket id, one send, reconciliation, review.

Perpetual futures can liquidate an account. HyperGrok is documentation and instructions, not financial advice.

## More

+ [README](../README.md) and [how the desk works](ARCHITECTURE.md)
+ Other tools: [Claude Code](hyperliquid-claude-code.md) · [Codex](hyperliquid-codex.md) · [Gemini CLI](hyperliquid-gemini-cli.md) · [Grok Build](hyperliquid-grok-build.md) · Grok Bot: [README Start](../README.md#start)
+ [FAQ](FAQ.md)
