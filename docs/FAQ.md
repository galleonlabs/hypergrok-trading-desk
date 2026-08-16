# FAQ

**What do I actually get?**
Seven Bots in your Grok Bot workspace, one Trading Floor group chat, sixteen shared skills, and a written way of working. Ask for a market brief, a sized trade, a backtest or a review, and the right Bot answers with live data and sources.

**What do I need to start?**
Grok Bot and the paste-in prompt from the README. The desk starts in research mode. Add a testnet API wallet when you want to practise (the testnet faucet funds any address that has ever deposited on mainnet); add a mainnet one when you decide to trade real money.

**How does the desk trade?**
Through a Hyperliquid API (agent) wallet you create in the Hyperliquid app and hand over through Grok Bot's secure secret card. It can place, modify and cancel orders. Withdrawals, transfers and bridging stay in the app with your main wallet.

**How do I approve a trade?**
The Risk Manager posts a ticket with an id like `HG-20260816-01`. You type `approve HG-20260816-01`. That exact phrase, after seeing that exact ticket, is what lets the Execution Trader send it, once, within thirty minutes.

**Where do the ideas come from?**
From you. The Strategist turns your idea into rules and tests it on Hyperliquid history with fees, funding and an out-of-sample split; the desk then paper-trades it on testnet if you like. The Market and Research Analysts give you the picture; you decide.

**Why do the Bots use a "computer"?**
That is Grok Bot's name for the cloud VM every account gets. Reads are plain `curl` against `/info`; the Execution Trader runs the official SDK there to sign orders with the API wallet. The browser is only used for reading pages.

**Why six on the floor and one off it?**
Grok Bot group chats hold six Bots, and reviews are calmer after the noise. The Trade Reviewer works from its own conversation and by DM.

**Can I rename the Bots or add my own?**
Yes. Names and avatars are yours. To add a role, copy the shape of a file in `agents/`. Keep one writer: only one Bot has the exchange write skills.

**Can it run while I sleep?**
Routines can brief you, watch prices and funding, check the book and alert you. Sending always waits for your approval by id.

**Which network does market data come from?**
Mainnet, even when you trade on testnet, because testnet books are thin. Every number says which network it came from.

**What if a send times out?**
The Execution Trader checks the exchange by client order id, tells you whether the order exists, and asks for a fresh approval only if it does not.

**How do I set Grok Bot's own approvals?**
Settings, General, Auto-review: add a Require Approval rule for financial actions and for commands that call the Hyperliquid exchange endpoint. Require Approval wins over Always Allow.

**Does it work outside Grok Bot?**
Yes. Grok Build, Cursor and Claude Code load the same `agents/`, `skills/` and `rules/` as a plugin.

**Is this financial advice?**
It is documentation and instructions. Perpetual futures can liquidate an account.
