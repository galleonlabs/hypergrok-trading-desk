# Codex Repository Guidance

## Product boundary

This repository builds an agent-runtime-neutral trading research and execution harness. The installable ChatGPT/Codex plugin is the primary agent interface and OpenCode is the compatible second interface; neither is the trading engine.

- Keep domain, validation, risk, admission, OMS, ledger, signer, and venue-adapter code independent of Codex, ChatGPT, Grok, Claude, or any model runtime.
- Put durable Codex working agreements here. The canonical packaged workflows live under `plugins/trading-desk/skills`; mirror them into `.agents/skills` with `scripts/sync_plugin_skills.py` for repository and OpenCode discovery.
- The `trading-desk` MCP server exposes exactly three reviewed tools: harness status, public market briefs, and schema/hash validation for proposed intents. All are read-only and none may load an account secret, create authorization, reserve risk, or call a venue write endpoint.
- Keep OpenCode permissions fail-closed in `opencode.json`; it may call only those exact local MCP tools and mirrored skills. Do not add a model/provider, write tool, custom agent with wider rights, or external-directory access without review.
- Do not use OpenCode `--auto` for this repository; it converts `ask`
  decisions into approvals. The checked-in profile intentionally denies
  unlisted shell commands.
- ChatGPT/Codex and OpenCode must call the same typed tool service. Protocol or model adapters may not reinterpret tool results or widen their capability.

## Capital boundary

- No agent, prompt, skill, webpage, generated script, or chat message may hold a signing key or call a venue write endpoint.
- No testnet or mainnet writes are enabled in the foundation.
- The foundation admits only local `infrastructure_testnet` `simulate_order`
  commands; deny strategy, shadow, mainnet, and systematic grants.
- Approval in chat is invalid.
- Evidence status and deployment authority are separate.
- Use exact `Decimal`/integer monetary arithmetic; reject binary floats for prices, sizes, fees, and limits.
- Admission must atomically reserve risk, consume a single-use command authorization, update policy counters, and create the durable outbox row before network I/O.
- Unknown outcomes remain reserved and are reconciled; never blindly resend.
- After exposure exists, only the account-safety policy may authorize bounded cancel/protect/flatten actions through the same serialized executor.

## Development workflow

- Python baseline: 3.11 or newer, standard library unless a reviewed dependency is justified.
- Run `python3 -m unittest discover -s tests -v` after changes.
- Run `python3 -m compileall -q src tests` before handoff.
- Run `python3 scripts/sync_plugin_skills.py --check` after changing a packaged skill.
- Run `python3 scripts/sync_plugin_runtime.py --check` after changing any module under `src/trading_harness`; the cached plugin runtime must be an exact generated mirror.
- Keep the venue executor disabled by default. Tests must prove writes fail closed.
- Update `docs/trading_harness_spec.md` when an invariant, state, authorization model, or promotion gate changes.
- Add tests for observable invariants and failure transitions, not wording.
- Preserve upstream provenance in `UPSTREAM.md`; do not copy legacy capital-path prompts or snippets back into runtime locations.

## Agent workflows

- Use `$operate-trading-desk` for multi-stage desk coordination.
- Use `$brief-market` for live public Hyperliquid market context through the typed MCP tool.
- Use `$validate-thesis` for strategy, indicator, backtest, and edge claims.
- Use `$scan-signals` for read-only registered-rule scans.
- Use `$test-strategy` for reproducible historical evaluation plans or supplied artifacts.
- No skill may issue orders, sizes, approvals, or deployment grants.

## Code review rules

- Flag any path from agent-controlled input to credentials, signer, venue writes, authorization mutation, or policy widening.
- Flag floats in monetary/risk calculations.
- Flag non-atomic risk check followed by reservation/outbox.
- Flag release of risk on an order fill without conversion to booked position exposure.
- Flag retries without an endpoint-specific idempotency and unknown-outcome contract.
- Flag environment-variable-only selection of mainnet or account.
- Flag tests that mock away the failure being claimed as covered.
