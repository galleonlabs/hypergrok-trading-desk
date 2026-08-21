# Contributing

This project is an agent-neutral trading research and execution harness. Codex is the first supported interface; models never sit on the capital-bearing path.

## Before changing code

Read:

- [`AGENTS.md`](AGENTS.md)
- [`docs/trading_harness_spec.md`](docs/trading_harness_spec.md)
- [`SECURITY.md`](SECURITY.md)

## Required checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

## Capital-path changes

Changes to domain schemas, evidence status, deployment grants, authorization, risk, reservations, OMS/outbox, signer, account-safety policy, adapters, or reconciliation require:

- An explicit invariant and failure model.
- Exact-decimal monetary handling.
- Negative tests proving forbidden paths remain denied.
- Crash/unknown-outcome tests where state crosses a network or persistence boundary.
- Updated specification and migration notes when behavior or stored state changes.
- Independent review before any environment grant is widened.

Do not add:

- Agent-readable signing credentials.
- Chat-based approval.
- Mainnet selection by environment variable alone.
- Mutable dependency ranges in a signer environment.
- Live venue writes without the relevant qualification and deployment grant.
- A strategy claim without the thesis-validation lifecycle.

## Provenance

Imports from upstream or another project must record source URL, commit, license, file/path, modifications, and disposition. Do not copy legacy capital-path prompts or snippets into runtime locations.
