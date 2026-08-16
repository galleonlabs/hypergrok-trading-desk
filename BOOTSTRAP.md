# Bootstrap HyperGrok

In Grok Build or Cursor, install or enable the Agent Plugin. In Grok Bot, first create the seven custom Bots and teach or upload their relevant skills by following [docs/GROK_BOT.md](docs/GROK_BOT.md).

Then send this to the owning Bot or agent:

> Run the `crew-bootstrap` skill. Form the seven-role HyperGrok desk from the bundled agent definitions: desk lead, market analyst, onchain analyst, risk officer, execution trader, portfolio manager and trade reviewer. Create persistent named Bots only if the product exposes that capability and verify them by reading the resulting Bot list. Otherwise use plugin agents or explicit role-separated passes and tell me which fallback is active. Run read-only doctor and BTC market checks against both Hyperliquid testnet and mainnet. Do not request a key, approve a fee, fund an account or submit an order.

Expected receipt:

+ seven roles listed with their actual runtime mode
+ testnet and mainnet endpoint checks
+ one market-analyst to risk-officer handoff
+ zero signing requests and zero order submissions

Grok Bot does not currently document arbitrary repository installation or a public API that lets a plugin create persistent Bots silently. The bootstrap therefore verifies what the active product actually supports rather than claiming a fictional one-click path. A bare GitHub URL is not an installation receipt.
