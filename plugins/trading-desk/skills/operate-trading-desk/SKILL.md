---
name: operate-trading-desk
description: Coordinate a trading-desk request across market evidence, thesis work, risk review, deterministic execution boundaries, and post-trade review. Use when a user asks the desk to manage a trade lifecycle or coordinate several trading roles; use a focused desk skill for one isolated task.
---

# Operate Trading Desk

Read [references/roles.md](references/roles.md) before assigning or describing desk work. Roles improve review and routing; they do not grant authority.

## Establish the boundary

1. Call `get_harness_status` before making a capability claim. Treat only the capabilities and write state it actually returns as authoritative. Check freshness on each data result; do not infer it from status.
2. Identify the requested instrument, venue, network, account scope, and lifecycle stage. Leave unresolved fields unresolved.
3. If a required interface is absent, disabled, stale, or inconsistent, stop that path and report the exact blocker. Never replace a missing harness function with generated shell commands, direct SDK calls, or free-form venue payloads.

## Route the work

- For current market state, use `$brief-market` and the typed `get_market_brief` tool.
- For a named asset watch or buy/sell/nothing assessment, use `$assess-asset` and the typed tracking, sentiment, analysis, validation, and node-status tools.
- For an idea or edge claim, use `$validate-thesis` before treating it as a registered research rule.
- For registered-rule observations, use `$scan-signals` and `analyze_asset`; a match is evidence, not an order.
- For historical evaluation, use `$test-strategy` and `validate_candidate_profitability`; preserve every attempted variant and require prospective shadow evidence after historical PASS.
- For an explicitly requested small TESTNET learning experiment, use the exact saved analysis with `stage_trade_candidate`. Preserve `profitability_qualified: false`, `mainnet_authorized: false`, `daily_loss_deferred_to_executor: true`, the mandatory stop, grant hash, daily-loss scope hash, expiry, and all blockers. Never describe the staged loss value as authoritative: only the isolated executor's complete same-tick refresh can permit entry. Staging is not approval or execution.
- For learning, use `get_learning_review` for one cycle and `get_learning_summary` for exact-version descriptive aggregates. Report fees, slippage, latency, fills, venue-reported PnL, missing outcome/path evidence, and the no-causality/no-future-profitability boundary.
- For a harness-produced candidate intent, call `validate_trade_intent`. It currently checks schema and canonical identity only; report that scope and its result unchanged. It does not perform portfolio risk review, approve, sign, or submit an order.

No skill or model is the execution venue. Execution belongs to the isolated deterministic TESTNET worker and may proceed only after the separate direct-terminal control plane consumes an exact active staged ticket. Approval in chat is not authorization; never type, relay, or simulate the confirmation for the user. Do not imply that an order was sent without immutable harness and venue records. Mainnet remains unavailable.

## Report

Return the current stage, evidence used with UTC timestamps, deterministic verdicts, unresolved risks, blocker or next safe step, and stable analysis/staging/cycle/command identifiers. Separate facts, calculations, and interpretation. Never expose credentials, request a private key, widen policy, or turn research output into trading authority.
