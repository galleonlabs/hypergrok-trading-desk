# Provenance

Everything here is original prose and original snippets, written against public documentation and open-source SDK code. The initial API verification was on 2026-08-16; later checks and their limits are recorded below.

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

## Hyperliquid CLI design review - 2026-10-08

Reviewed [hypurrclaw/hyperliquid-cli](https://github.com/hypurrclaw/hyperliquid-cli/tree/5d950fff8b8c1fcd9792132614fec103476f666f), version 0.11.1, at commit `5d950fff8b8c1fcd9792132614fec103476f666f`. Its command contracts and explicit DEX resolution informed the original stdlib JSON diagnostics, offline contract discovery and read-only HIP-3 snapshots in this release. No third-party code, prose, fixture, wallet backend, fee default or execution loop was copied.

The review exposed two existing inconsistencies: float-first spot formatting and an incorrect claim that a newer nonce invalidates all lower nonces. Corrections were checked against the official [tick and lot rules](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size), [nonce model](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets), [noop action](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#invalidate-pending-nonce-noop) and [info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint). Decimal values are preserved through the published helpers and checked against Python SDK 0.24.0's `float_to_wire` before the signing boundary. Offline regression tests exercise approved price bounds, structured failures, source identity, delisted/ambiguous markets and explicit DEX requests. Live proof uses public `/info` reads only; no key or signed action is used.

## Research dataset contract — 2026-10-10

The original research collector and offline replay verifier were designed against the official [info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [perpetuals metadata and history](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals), [funding rules](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) and [rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits). The references establish the recent-candle ceiling, canonical market identity, history pagination, hourly funding and oracle-based payment arithmetic. No external collector, fixture or strategy implementation was copied.

[Research datasets](RESEARCH_DATASETS.md) distinguishes exchange behavior from local controls: explicit closed UTC ranges, strict data validation, finite page budgets, duplicate conflict handling, saved decoded responses, coverage gaps and offline replay. Hour-bucket funding coverage preserves actual millisecond event times. Historical research can retain a currently delisted market; current metadata is not a historical universe. Complete coverage does not establish source authenticity, exact funding PnL, an unbiased experiment or Risk PASS.

This source prepares an unpublished candidate. Updated manifest hashes do not prove a tagged release, native skill installation, public export or marketplace acceptance. The 1.5.0 native publication record in [Grok Bot template](GROK_BOT_TEMPLATE.md) is retained as evidence for that earlier release.
