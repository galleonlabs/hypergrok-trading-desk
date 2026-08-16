# Changelog

All notable changes to HyperGrok are recorded here.

## 2.0.0 - 2026-08-16

HyperGrok is now instructions and resources for a user's Grok Bot, not a Python CLI.

### Removed

+ The `hypergrok` Python package, CLI, tests, packaging, `.env` configuration and verification harnesses.
+ The `onchain-analyst` and `portfolio-manager` roles (folded into Research Analyst and Risk Manager) and the CLI-bound skills.

### Added

+ `SETUP.md`: the single file a Grok Bot follows to build the desk - repository onto the computer, seven Bots from profile cards, sixteen shared skills, one Trading Floor group chat, approval rules, desk record, read-only verification, receipt.
+ Seven roles with full system prompts: Desk Lead, Market Analyst, Research Analyst, Strategist, Risk Manager, Execution Trader, Trade Reviewer.
+ Eight Hyperliquid skills that teach Bots to work with the exchange directly through `curl` and the official SDKs: setup and API wallets, market data, account reads, orders, positions and margin, WebSocket, advanced actions, and an API reference.
+ Eight desk skills: operating model, trade lifecycle, risk limits and sizing, execution protocol, monitoring, post-trade review, incident response, strategy lab.
+ `docs/FAQ.md`; rewritten `docs/ARCHITECTURE.md` and `docs/PROVENANCE.md`.
+ `scripts/check.sh` and a CI workflow that lints frontmatter, links and stale CLI references.

### Changed

+ Safety model: from code gates in a CLI to desk procedure (ticket, Risk PASS, approval by id, single send, reconciliation) plus Hyperliquid API-wallet permissions and Grok Bot approval controls.
+ Plugin manifests, rules and README rewritten for the new shape.

## 1.0.0 - 2026-08-16

+ Initial release as a Python CLI and Agent Plugin.
