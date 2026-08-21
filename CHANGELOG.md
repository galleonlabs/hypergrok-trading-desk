# Changelog

## Unreleased — Harness foundation

- Replaced the Grok Bot prompt/plugin runtime with an agent-neutral deterministic harness foundation.
- Added Codex repository guidance and focused `validate-thesis` and `scan-signals` skills.
- Added canonical semantic intents, evidence/deployment separation, policy/admission scaffolding, durable outbox/reservation design, and a fail-closed executor boundary.
- Added the fork provenance record, source audit matrix, and normative harness specification.
- Removed legacy agent prompts, write skills, plugin manifests, mutable setup path, and upstream branding from runtime locations. The exact upstream snapshot remains available through Git history and recorded object IDs.

## Upstream history

The fork began from Galleon Labs current-main commit `62cbe227a2ec531e0efa37254d4b6fae043fbfe5`. Its upstream changelog and disconnected Python `v1.0.0` lineage are audit evidence, not releases of this harness. See [`UPSTREAM.md`](UPSTREAM.md).
