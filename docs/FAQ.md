# FAQ

**Is this a bot that trades for me?**
No. It is a set of instructions your own Grok Bot uses to build a desk of specialist Bots. The desk researches, sizes, executes what you approve, and reviews. It never sends an order you did not approve by ticket id.

**Does it come with strategies?**
No, on purpose. The Strategist helps you turn your own ideas into rules and test them honestly. Nothing here tells you what to trade or promises a return.

**Do I need a Hyperliquid account or key to start?**
No. The desk starts as a research desk. You add a testnet API wallet when you want to practise, and a mainnet one when you decide to trade real money.

**Why an API wallet and not my main wallet?**
An API (agent) wallet can trade but cannot withdraw, transfer or bridge. Your Bots share one cloud computer, so anything on it is readable by all of them; a trade-only key bounds the damage. Never give any Bot a seed phrase or main-wallet key.

**How does the key get onto the computer?**
Through Grok Bot's secure secret card, named `HYPERLIQUID_PRIVATE_KEY`. Never paste it in chat. If your setup does not expose secrets as environment variables, you create a `~/.hyperliquid/api-wallet.key` file yourself while in control of the computer (`hyperliquid-setup` section 4).

**Why do the Bots use a computer at all? Isn't this just an API?**
It is an API. "Computer" is Grok Bot's name for the cloud VM every account gets. Signing an order needs code and a key, so the Execution Trader runs the official SDK from that computer's terminal. Reads are plain `curl`. The browser is only used by the Research Analyst to read pages.

**Why six Bots on the floor and one off it?**
Grok Bot group chats hold up to six Bots. Reviews are calmer after the noise anyway, so the Trade Reviewer works from its own conversation and by direct message.

**Can I rename the Bots or add my own?**
Yes. Names are yours. Keep the one-writer rule: only one Bot has the exchange write skills, and it sends only on approved tickets. To add a role, copy the shape of a file in `agents/` (profile card, system prompt, boundaries, handoff format).

**Can the desk run unattended?**
Routines can read, brief and alert. Nothing sends without your approval by id. If you want unattended execution, this is not the tool.

**What does "approve HG-20260816-01" actually do?**
It is the only phrase that lets the Execution Trader send that exact ticket, once, within its expiry. "Yes" or a thumbs-up does not count, so a stray reply cannot fire an order.

**How do I set Grok Bot's own approvals?**
Settings, General, Auto-review: add a Require Approval rule for financial actions and for commands that call the Hyperliquid exchange endpoint. Require Approval wins over Always Allow. Never add an Always Allow rule for exchange writes.

**Testnet or mainnet market data?**
The desk reads market data from mainnet even when it trades on testnet, because testnet books are thin, and it always says which network a number came from.

**What happens if a send times out?**
The Execution Trader does not resend. It queries the exchange by client order id, reports whether the order exists, and asks for a fresh approval only if it does not. See `desk-incident-response`.

**Can I use this in Grok Build, Cursor or Claude Code instead of Grok Bot?**
Yes. The repository is also a plugin: `agents/`, `skills/` and `rules/` load directly. Group chats become subagents or role-labelled passes.

**Is this financial advice?**
No. It is software documentation. Perpetual futures can liquidate an account.
