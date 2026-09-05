<p align="center">
  <img src="assets/mascot-320.jpg" width="160" alt="HyperGrok mascot">
</p>

<h1 align="center">HyperGrok</h1>

<p align="center"><strong>Turn your Grok Bot into a 7-agent Hyperliquid trading desk.</strong></p>

Add the public Desk Lead to [Grok Bot](https://x.ai/bot/PReCwAHq8Vgeex50r883H). It opens with a live, zero-key Hyperliquid market snapshot, builds research, risk, execution and review as separate agents, then proves the floor is ready. They brief you on markets, size your trades, send what you approve and tell you honestly how it went. You bring the ideas. Let them cook.

## Start

<p align="center">
  <a href="https://x.ai/bot/PReCwAHq8Vgeex50r883H">
    <img src="https://img.shields.io/badge/Add%20to-Grok%20Bot-000000?style=for-the-badge" alt="Add HyperGrok to Grok Bot">
  </a>
</p>

Click **Add to Grok Bot**, then send **HyperGrok Desk Lead** one message:

> Start the desk.

That is the supported one-click, one-message install. The public template carries all seventeen reviewed skills as version-pinned pointers, with no memories, routines or plugins.

### Manual fallback

If the public template is unavailable, open Grok Bot and paste this to any Bot:

> Set up the HyperGrok trading desk from https://github.com/galleonlabs/hypergrok-trading-desk/blob/v1.4.2/skills/hypergrok-bootstrap/SKILL.md. Follow the bootstrap skill, use https://github.com/galleonlabs/hypergrok-trading-desk/blob/v1.4.2/SETUP.md for the complete runbook, and finish with its evidence receipt.

The desk starts in research mode. The first demo uses only Hyperliquid's public `/info` endpoint: no wallet, account read or order. Add a testnet API wallet when you want to practise with play money, and a mainnet one when you are ready.

### Opening Bell

The first thing the Desk Lead shows is useful, live output—not a configuration form:

```bash
python3 scripts/opening_bell.py --coin ETH
```

It reports source and UTC time, mid/mark/oracle, 24-hour change and volume, hourly funding, open interest, spread and depth at 5/10/25 bps. Twenty book levels stop a few bps from the mid on a liquid perp, so it re-reads the book at a coarser `nSigFigs` page until each band is measured, names the page every figure came from, and still marks a band no page reaches as a floor rather than a total. `scripts/desk_doctor.py` then checks the release, team files, desk folders and public connectivity. Both are read-only and standard-library Python.

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
market ETH-PERP  side buy  size 0.4827 ETH (~$1,448)
entry limit 3,000 Gtc     stop sell 0.4827 @ 2,900 market (sent with the entry)
risk $51.00 = 0.5% of equity $10,200 (clearinghouseState 14:11 UTC)  R = 100
sized on a stressed stop: 100 + 3.00 slippage + 2.65 fees = 105.65 per ETH
approve with: "approve HG-20260816-01"
```

You type `approve HG-20260816-01`. The Execution Trader sends entry and stop in one grouped order, reads the exchange response, confirms it on the book, and reports the order ids. When it closes, the Trade Reviewer tells you what it cost, whether the process was clean, and one thing worth keeping.

**"Can we test whether funding extremes mean-revert?"** The Strategist writes the rules with you, pulls a year of candles and funding, runs a backtest with fees and an out-of-sample split, and reports the trade distribution, not a headline number. Like it? Paper-trade it on testnet through the same desk.

**"Watch ETH and tell me if funding flips negative."** A routine on the desk computer watches the WebSocket feed and pings you when it happens.

## What the desk knows

Seventeen skills, in the portable `SKILL.md` format, shared by all your Bots.

**Bootstrap** - pinned release install, Opening Bell, team construction, desk doctor and a receipt that distinguishes what happened from what still needs a manual step.

**Hyperliquid** - setup and API wallets, market data, account state, orders (limit, IOC, take-profit and stop-loss with grouping, client order ids), positions and margin, WebSocket feeds, advanced actions (dead-man's switch, TWAP, spot), and a compact API reference. Copy-pasteable `curl` for reads; the official Python SDK and `@nktkas/hyperliquid` for anything that signs.

**Desk** - how the team works: operating model, the trade lifecycle and ticket, risk limits and sizing arithmetic, the execution protocol, monitoring and routines, post-trade review, incident playbooks, and the strategy lab.

## Built for real money

- **You approve every trade**, by ticket id, after seeing the exact order. The line you type is evidence; the gate that enforces it sits outside the chat, in Grok Bot's own Require Approval rule, because a Bot that can read an approval could also write one.
- **A trade-only API wallet**, provisioned through Grok Bot's secure secret store, is the only key the desk ever holds. It can trade; it cannot withdraw.
- **Sized on a stressed stop.** A triggered stop is a market order: it slips and pays taker on both legs. The desk sizes on what the stop will actually cost, not its trigger price, so your risk budget means what it says.
- **Ceilings you cannot trade through.** Your limits file may only tighten the desk's own caps, never loosen them.
- **Testnet first.** Every new kind of action is rehearsed with play money before it touches mainnet.
- **One writer.** Six Bots read; one Bot sends, once per approval, and reconciles by client order id.
- **A reviewer who keeps you honest.** Process and outcome graded separately, in a journal you can read.

Perpetual futures can liquidate an account. HyperGrok is documentation and instructions, not financial advice.

## Also runs in Grok Build, Cursor and Claude Code

The same `agents/`, `skills/` and `rules/` load as a plugin: seventeen skills, and the seven roles as subagents.

In Claude Code, install it from this repository:

```
/plugin marketplace add galleonlabs/hypergrok-trading-desk
/plugin install hypergrok@hypergrok
```

In Grok Build and Cursor, open the repository and enable the plugin.

The same pack installs as a skill:

```
npm exec --package=skills@1.5.23 -- skills add galleonlabs/hypergrok-trading-desk
```

Live listings: [skills.sh](https://www.skills.sh/galleonlabs/hypergrok-trading-desk) · [cursor.directory](https://cursor.directory/plugins/hypergrok) · [botdirectory.ai](https://botdirectory.ai/bots/hypergrok-trading-desk)

Either way, run `/desk-operating-model` to begin.

## Inside the repository

```
SETUP.md     what your Grok Bot follows to build the desk
agents/      seven roles: Bot profile card + full system prompt
skills/      seventeen skills (bootstrap, hyperliquid-*, desk-*)
template/    exact public Grok Bot profile and skill hashes
scripts/     zero-key Opening Bell, desk doctor and release checks
docs/        how it works, FAQ, provenance
assets/      the mascot - use it as your Bots' avatar
```

| Doc | |
| --- | --- |
| [How the desk works](docs/ARCHITECTURE.md) | roles, files, trust boundaries |
| [FAQ](docs/FAQ.md) | keys, approvals, testnet, customising the team |
| [Skills index](skills/README.md) | every skill and who uses it |
| [Grok Bot template](docs/GROK_BOT_TEMPLATE.md) | public profile, publish contract and clean-install evaluation |
| [Provenance](docs/PROVENANCE.md) | sources and licences |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) | |

<p align="center">
  <img src="https://pump.fun/pump-logomark.svg" width="52" alt="pump.fun"><br>
  <strong>Supported by the pump.fun community</strong>
</p>

## License and credit

[MIT licensed](LICENSE), with the copyright and permission notice retained when reusing copies or substantial portions. Created by [Andrew Wilkinson](https://andrewwilkinson.io) and [Galleon Labs](https://github.com/galleonlabs).

See [reuse and attribution](ATTRIBUTION.md) for a ready-to-copy credit line. If HyperGrok Trading Desk helps your work, [a star on the original repository](https://github.com/galleonlabs/hypergrok-trading-desk) is appreciated and entirely optional.
