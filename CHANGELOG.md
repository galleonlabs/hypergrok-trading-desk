# Changelog

## Unreleased — Harness foundation

- Replaced the Grok Bot prompt/plugin runtime with an agent-neutral deterministic harness foundation.
- Added a ChatGPT/Codex-first `trading-desk` plugin with five agent-neutral skills and a byte-identical OpenCode mirror.
- Added a model-neutral read-only tool service plus MCP 2.0 stdio and loopback Streamable HTTP adapters.
- Added explicit closed MCP input/output schemas, server-side duplicate validation, and a self-contained generated plugin runtime for cached installs.
- Added an allowlisted public Hyperliquid market brief with exact decimals, source/receipt timestamps, freshness gates, and 5/10/25 bps depth.
- Added fail-closed harness-status and semantic-intent validation/hash tools; no tool can authorize, sign, or write to a venue.
- Added OpenCode configuration for the same three exact MCP tools without selecting a model provider.
- Added canonical semantic intents, evidence/deployment separation, policy/admission scaffolding, durable outbox/reservation design, and a fail-closed executor boundary.
- Added the fork provenance record, source audit matrix, and normative harness specification.
- Removed legacy agent prompts, write skills, mutable setup path, and upstream branding from runtime locations. Selected research/desk knowledge was rewritten into the new plugin without key loaders or direct exchange-write snippets. The exact upstream snapshot remains available through Git history and recorded object IDs.

## Upstream history

The fork began from Galleon Labs current-main commit `62cbe227a2ec531e0efa37254d4b6fae043fbfe5`. Its upstream changelog and disconnected Python `v1.0.0` lineage are audit evidence, not releases of this harness. See [`UPSTREAM.md`](UPSTREAM.md).
