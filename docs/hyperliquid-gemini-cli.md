# Hyperliquid trading desk for Gemini CLI

HyperGrok is a Gemini CLI extension for Hyperliquid: seventeen agent skills for research, risk, execution and review, with seven roles run as labelled passes. It is open source (MIT), starts read-only on public market data, and sends an order only for a ticket you approve by id with a trade-only API wallet you provide.

## Install

```bash
gemini extensions install https://github.com/galleonlabs/hypergrok-trading-desk
```

Gemini CLI installs the latest GitHub release. The extension manifest is `gemini-extension.json`. Manifest validation is covered by the repository checks; a native installation of the current release has not been verified.

Load `desk-operating-model` before asking the roles to act.

## What loads

+ **Seventeen skills**: Hyperliquid setup, market data, account state, orders, positions, WebSocket feeds, advanced actions and a compact API reference, plus the desk skills for the trade lifecycle, risk limits and sizing, execution, monitoring, post-trade review, incidents and the strategy lab.
+ **`GEMINI.md`**, loaded every session. It names the roles and repeats the desk rule's approval paragraph word for word: analysis is not approval, and a send needs your exact approval phrase with the ticket id.
+ **Roles as labelled passes.** This extension does not register the role files as native subagents. Run the seven roles in `agents/` as labelled passes using their system prompts. Only the Execution Trader pass writes to the exchange.

The Claude Code exchange-write hook does not load in Gemini CLI.

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
+ Other tools: [Claude Code](hyperliquid-claude-code.md) · [Cursor](hyperliquid-cursor.md) · [Codex](hyperliquid-codex.md) · [Grok Build](hyperliquid-grok-build.md) · Grok Bot: [README Start](../README.md#start)
+ [FAQ](FAQ.md)
