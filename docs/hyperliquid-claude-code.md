# Hyperliquid trading desk for Claude Code

HyperGrok turns Claude Code into a Hyperliquid trading desk: seven roles as subagents and seventeen agent skills for research, risk, execution and review. It is open source (MIT), starts read-only on public market data, and sends an order only for a ticket you approve by id with a trade-only API wallet you provide.

## Install

In Claude Code:

```
/plugin marketplace add galleonlabs/hypergrok-trading-desk
/plugin install hypergrok@hypergrok
```

Then start with the desk's operating model:

```
/hypergrok:desk-operating-model
```

## What loads

+ **Seventeen skills**: Hyperliquid setup, market data, account state, orders, positions, WebSocket feeds, advanced actions and a compact API reference, plus the desk skills for the trade lifecycle, risk limits and sizing, execution, monitoring, post-trade review, incidents and the strategy lab.
+ **Seven roles as subagents** from `agents/`: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader and Trade Reviewer. Only the Execution Trader writes to the exchange.
+ **The desk process** from `desk-operating-model`: load it before delegating work. The repository's `rules/` directory is declared for Cursor; Claude Code does not automatically load it as a plugin component.
+ **An exchange-write hook.** Claude Code asks for confirmation when a Bash command or a named script matches a Hyperliquid write. The hook does not follow imports or cover every possible route, and it cannot bind a send to a ticket. It supplements the ticket process and verified native approval coverage.

## First run, no keys

Ask the desk for a market brief, for example "Brief me on ETH". The reads use Hyperliquid's public `/info` endpoint, with no wallet or account. The same read works from a terminal:

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
+ Other tools: [Cursor](hyperliquid-cursor.md) · [Codex](hyperliquid-codex.md) · [Gemini CLI](hyperliquid-gemini-cli.md) · [Grok Build](hyperliquid-grok-build.md) · Grok Bot: [README Start](../README.md#start)
+ [FAQ](FAQ.md)
+ [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference)
