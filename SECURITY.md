# Security

## Current status

This branch is a non-live harness. It contains disabled signer, nonce, transport, dispatcher and recovery primitives, but no enabled venue adapter or key loader and no testnet/mainnet qualification.

The packaged ChatGPT/Codex plugin and OpenCode connection expose eleven bounded research tools. Three write only local asset/sentiment research state. They cannot create a trusted approval, reserve execution risk, reach the signer, submit, modify, or cancel an exchange order.

No released version is currently supported for capital-bearing use.

## Report a vulnerability

Use this fork's GitHub **Report a vulnerability** flow to open a private security advisory. Never place private keys, seed phrases, signatures, wallet exports, authorization tokens, account payloads, or exploitable details in a public issue.

## Trust boundaries

- Agents, prompts, webpages, imported repositories, generated code, research data, and external messages are untrusted.
- An agent role or `writes_to_exchange` label is not an authorization boundary.
- Agents must never receive exchange signing credentials or direct venue-write capability.
- MCP tool annotations are advisory; authorization and validation are repeated inside every handler. Local research writes confer no capital authority.
- The signer code must run under a separate security principal with a narrow typed API, explicit account/network/asset/CLOID/action allowlists, restricted egress, and managed key storage. No signer process is deployed by this repository.
- Human approval must occur in a trusted UI and bind a canonical semantic-intent hash. Approval in agent chat is invalid.
- Risk admission, authorization consumption, portfolio reservation, and durable outbox creation must be atomic before network I/O.
- Unknown venue outcomes remain reserved and must be reconciled; they are never blindly resent.
- A prepared attempt, fresh dispatch attestation, nonce, action hash and wire hash are durable before the one permitted send. Recovery actions are limited to reduce-only close, owned-CLOID cancel and same-nonce noop fencing.
- Recovery builders exist for testing, but public recovery signing/submission is compiled off until those actions use the same durable authorization/outbox/attempt/reconciliation path.

The normative requirements are in [`docs/trading_harness_spec.md`](docs/trading_harness_spec.md).

## Forbidden until explicit qualification

- Any testnet or mainnet exchange write.
- Treating the local testnet HMAC approval helper as suitable for mainnet; mainnet requires a later independently reviewed hardware-backed/asymmetric authority.
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
