# Hyperliquid trading desk for Codex

HyperGrok gives Codex a Hyperliquid trading desk: seventeen agent skills for research, risk, execution and review, with seven roles run as labelled passes. It is open source (MIT), starts read-only on public market data, and sends an order only for a ticket you approve by id with a trade-only API wallet you provide.

## Install

```bash
codex plugin marketplace add galleonlabs/hypergrok-trading-desk
codex plugin add hypergrok@hypergrok
```

Codex reads `.agents/plugins/marketplace.json`, which pins the reviewed release. The [release history](../CHANGELOG.md#161---2026-10-13) records Codex CLI 0.160.0 installing the preceding release and listing all seventeen skills. That test does not establish native acceptance of a later release.

Load `desk-operating-model` before asking the roles to act.

## What loads

+ **Seventeen skills**: Hyperliquid setup, market data, account state, orders, positions, WebSocket feeds, advanced actions and a compact API reference, plus the desk skills for the trade lifecycle, risk limits and sizing, execution, monitoring, post-trade review, incidents and the strategy lab.
+ **Roles as labelled passes.** This plugin's Codex entry installs skills without registering `agents/` as native subagents. Run the seven roles as labelled passes using each role's system prompt. Only the Execution Trader pass writes to the exchange.

The Claude Code exchange-write hook does not load in Codex. The desk's approval by ticket id still applies.

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
+ Other tools: [Claude Code](hyperliquid-claude-code.md) · [Cursor](hyperliquid-cursor.md) · [Gemini CLI](hyperliquid-gemini-cli.md) · [Grok Build](hyperliquid-grok-build.md) · Grok Bot: [README Start](../README.md#start)
+ [FAQ](FAQ.md)
