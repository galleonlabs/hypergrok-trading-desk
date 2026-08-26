# Security

## Current status

This branch has no configured or qualified live account. It contains an armed
TESTNET-only signer, one-shot transport, entry/recovery dispatchers, typed
reconciliation, and a macOS Keychain reader. Mainnet is hard-disabled and no
Codex/MCP tool can reach the execution boundary.

The packaged ChatGPT/Codex plugin and OpenCode connection expose fifteen
bounded research/learning tools. Five write only local research, analysis,
sentiment or all-false-authority staging state. They cannot create a trusted
approval, reserve execution risk, reach the signer, submit, modify, or cancel
an exchange order.

No released version is currently supported for capital-bearing use.

## Report a vulnerability

Use this fork's GitHub **Report a vulnerability** flow to open a private security advisory. Never place private keys, seed phrases, signatures, wallet exports, authorization tokens, account payloads, or exploitable details in a public issue.

## Trust boundaries

- Agents, prompts, webpages, imported repositories, generated code, research data, and external messages are untrusted.
- An agent role or `writes_to_exchange` label is not an authorization boundary.
- Agents must never receive exchange signing credentials or direct venue-write capability.
- MCP tool annotations are advisory; authorization and validation are repeated inside every handler. Local research writes confer no capital authority.
- The signer code must run under a separate security principal with a narrow typed API, explicit account/network/asset/recovery-CLOID/action allowlists, restricted egress, and managed key storage. Checked-in service templates do not provision or qualify that principal.
- Human approval must occur in the separate operator control plane and bind the exact staged risk ticket. The current TESTNET CLI reads confirmation directly from `/dev/tty`; approval in agent chat or piped stdin is invalid.
- `/dev/tty` is an attended TESTNET gesture, not cryptographic proof of a
  human. Running an agent shell under the control/executor UID is unsupported;
  production separation depends on distinct OS identities, file ACLs and
  Keychain ACLs. Mainnet would require independent hardware-backed user
  presence/MFA rather than this HMAC/TTY mechanism.
- Risk admission, authorization consumption, portfolio reservation, and durable outbox creation must be atomic before network I/O.
- Unknown venue outcomes remain reserved and must be reconciled; they are never blindly resent.
- A prepared attempt, fresh dispatch attestation, nonce, action hash and wire hash are durable before the one permitted send. Recovery actions are limited to reduce-only close, owned-CLOID cancel and same-nonce noop fencing.
- Entry submission additionally holds a revocable runtime guard across final
  authority consumption and the bounded one-shot send. Shutdown/halt before
  that point blocks transmission; afterward the attempt is allowed to finish
  and must be reconciled as the point of no return.
- Recovery close, role-aware cancel and same-nonce noop use the same durable
  permit/outbox/attempt/transport/reconciliation path. Every result still
  requires fresh venue/account reconciliation and unknown outcomes are never retried.
- Signer, approval, recovery and grant Keychain items are distinct. Dynamic
  entry/stop/target CLOIDs are trusted only from the immutable three-leg plan;
  recovery-close CLOIDs are independently derived from the incident and fresh
  position snapshot and revalidated inside the live signer.
- The agent/MCP identity cannot open executor-private execution, nonce,
  daily-loss or control-socket state. Only staging and learning databases live
  in the narrowly ACL-scoped shared-learning directory; agent quotes defer the
  authoritative daily-loss decision to the executor's same-cycle refresh.
- The attended control identity may write the execution database and shared
  staging/learning state required for exact authorization, but it receives no
  directory capability for nonce, daily-loss or control-socket state.

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
