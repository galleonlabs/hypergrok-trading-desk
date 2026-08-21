# Security

## Current status

This branch is a non-live harness foundation. It contains no enabled venue adapter, loads no exchange key, and is not approved for testnet or mainnet execution.

The packaged ChatGPT/Codex plugin and OpenCode connection expose only fail-closed status, public Hyperliquid `/info` market briefs, and semantic-intent schema/hash validation. These tools cannot authorize, sign, submit, modify, or cancel an order.

No released version is currently supported for capital-bearing use.

## Report a vulnerability

Use this fork's GitHub **Report a vulnerability** flow to open a private security advisory. Never place private keys, seed phrases, signatures, wallet exports, authorization tokens, account payloads, or exploitable details in a public issue.

## Trust boundaries

- Agents, prompts, webpages, imported repositories, generated code, research data, and external messages are untrusted.
- An agent role or `writes_to_exchange` label is not an authorization boundary.
- Agents must never receive exchange signing credentials or direct venue-write capability.
- MCP tool annotations are advisory; authorization and validation must be enforced inside every tool handler. The current three tools are read-only by construction.
- A future signer must run under a separate security principal with a narrow typed API, action allowlists, restricted egress, and managed key storage.
- Human approval must occur in a trusted UI and bind a canonical semantic-intent hash. Approval in agent chat is invalid.
- Risk admission, authorization consumption, portfolio reservation, and durable outbox creation must be atomic before network I/O.
- Unknown venue outcomes remain reserved and must be reconciled; they are never blindly resent.

The normative requirements are in [`docs/trading_harness_spec.md`](docs/trading_harness_spec.md).

## Forbidden until explicit qualification

- Any testnet or mainnet exchange write.
- Any persisted grant other than local `infrastructure_testnet` simulation.
- Loading an API-wallet or main-wallet key.
- Transfers, withdrawals, bridges, vault/subaccount fund movement, builder fees, or staking actions.
- Enabling an adapter by environment variable alone.
- Running copied upstream snippets against an account.
- Treating an agent, backtest, indicator match, or social post as deployment authorization.

## If a credential is exposed

1. Revoke it at the venue immediately.
2. Halt new admission and preserve existing protective exits.
3. Reconcile orders, fills, positions, and non-funding ledger changes from the last known-good point.
4. Rotate affected credentials and service identities.
5. Preserve redacted evidence and open a private incident review.

## Supported versions

| Version | Capital-bearing support |
| --- | --- |
| Unreleased foundation | No |
