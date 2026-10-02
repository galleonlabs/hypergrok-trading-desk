# Provenance

Everything here is original prose and original snippets, written against public documentation and open-source SDK code and verified against the live API on 2026-08-16.

| Source | Used for | Licence |
| --- | --- | --- |
| [Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs) | every endpoint, action, field, limit and error string in the `hyperliquid-*` skills | public docs |
| [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) 0.24.0 | Python calls in the skills; the SDK the desk installs | MIT |
| [@nktkas/hyperliquid](https://github.com/nktkas/hyperliquid) 0.33.3 | TypeScript calls and formatting helpers | MIT |
| [decimal.js](https://github.com/MikeMcl/decimal.js) 10.6.0 | Exact decimal arithmetic in the TypeScript bound example; install separately | MIT |
| [Grok Bot docs](https://docs.x.ai/grok-bot), [Cursor help](https://cursor.com/help/grok-bot) | Bots, group chats, shared computer, skills, routines, approvals, secrets | public docs |
| [Grok Build skills and plugins](https://docs.x.ai/build/features/skills-plugins-marketplaces), [Agent Skills spec](https://agentskills.io) | plugin layout and `SKILL.md` conventions | public docs |
| [Senpi skills](https://github.com/Senpi-ai/senpi-skills), [cezar-r/hyperliquid-skills](https://github.com/cezar-r/hyperliquid-skills), [Hermes hyperliquid skill](https://github.com/NousResearch/hermes-agent/tree/main/optional-skills/blockchain/hyperliquid) | structure survey only; nothing reused | MIT / Apache-2.0 declarations |
| kaileycompact51/HyperLiquid-Claw | reviewed and not used; it distributes an unverified Windows binary and an obfuscated install script | claimed MIT |

Order price/size precision was rechecked against the official [tick and lot size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size) contract on 2026-09-21. `scripts/test_order_rounding.py` executes only the published pure Decimal helpers, including buy-ceiling and sell-floor cases. No signed order was submitted for this validation.

## Fork adaptations - 2026-10-02

The [fork review](FORK_REVIEW.md) records all observed heads and dispositions. Ideas from [thysmans/hypergrok-iterated](https://github.com/thysmans/hypergrok-iterated), [jawndiego/hypergrok-trading-desk](https://github.com/jawndiego/hypergrok-trading-desk) and [shadowslayerai/hypergrok-trading-desk](https://github.com/shadowslayerai/hypergrok-trading-desk) were adapted into original prose; no third-party code, hooks or skill bodies were copied. Existing MIT notices remain intact.

Account abstraction and `userRole` response fields, subscription acknowledgements, snapshot fills and fill identifiers were rechecked against the official [info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) and [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) references on 2026-10-02. Reservations and reconnect readiness are desk controls, not exchange guarantees. No key or signed order was used for this review.
