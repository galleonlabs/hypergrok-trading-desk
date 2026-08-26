# Codex Repository Guidance

## Product boundary

This repository builds an agent-runtime-neutral trading research and execution harness. The installable ChatGPT/Codex plugin is the primary agent interface and OpenCode is the compatible second interface; neither is the trading engine.

- Keep domain, validation, risk, admission, OMS, ledger, signer, and venue-adapter code independent of Codex, ChatGPT, Grok, Claude, or any model runtime.
- Put durable Codex working agreements here. The canonical packaged workflows live under `plugins/trading-desk/skills`; mirror them into `.agents/skills` with `scripts/sync_plugin_skills.py` for repository and OpenCode discovery.
- The `trading-desk` MCP server exposes exactly fifteen reviewed research/learning tools. `track_asset`, `pause_tracked_asset`, `record_manual_sentiment`, `analyze_asset`, and `stage_trade_candidate` write only local research, learning, or non-authoritative staging state. None may load an account signing secret, create approval/capital authority, reserve risk, sign, or call a venue write endpoint.
- Keep OpenCode permissions fail-closed in `opencode.json`; it may call only those exact local MCP tools and mirrored skills. Local research writes remain `ask`, plan mode cannot call them, and no model/provider, execution tool, custom agent with wider rights, or external-directory access may be added without review.
- Do not use OpenCode `--auto` for this repository; it converts `ask`
  decisions into approvals. The checked-in profile intentionally denies
  unlisted shell commands.
- ChatGPT/Codex and OpenCode must call the same typed tool service. Protocol or model adapters may not reinterpret tool results or widen their capability.

## Capital boundary

- No agent, prompt, skill, webpage, generated script, or chat message may hold a signing key, approve a trade, or call a venue write endpoint.
- The isolated TESTNET worker has a deployable write path, but it is a separate process and CLI. It is never an MCP tool or skill capability. Mainnet remains hard-disabled.
- The foundation admits only local `infrastructure_testnet` `simulate_order`
  commands; deny strategy, shadow, mainnet, and systematic grants.
- Approval in chat is invalid. `stage_trade_candidate` can create only an all-false authority document; attended authorization requires the exact ticket confirmation read directly from `/dev/tty` by `trading-harness-executor`.
- Evidence status and deployment authority are separate.
- Use exact `Decimal`/integer monetary arithmetic; reject binary floats for prices, sizes, fees, and limits.
- Admission must atomically reserve risk, consume a single-use command authorization, update policy counters, and create the durable outbox row before network I/O.
- Unknown outcomes remain reserved and are reconciled; never blindly resend.
- After exposure exists, only the account-safety policy may authorize bounded cancel/protect/flatten actions through the same serialized executor.
- The agent/MCP identity must never open or receive filesystem access to the
  executor's execution, nonce, daily-loss or control-socket state. Agent quotes
  defer daily loss; an entry needs a complete refresh capability minted by the
  executor in that same tick.
- Parent and recovery reconciliation must use exact venue-server source
  watermarks, canonical fill identities and one globally continuous owned-fill
  chain. Local mutation lease time is separate from venue evidence time.

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

## Deployment networking and commissioning

- Read `docs/ubuntu_vm_router.md` before creating or changing an executor
  network path. The checked-in Ubuntu bundle is `local_nat_lab`: it keeps the
  executor on macOS, does not change the public IP, does not prevent macOS host
  bypass, and is not VPN or mainnet qualification.
- Read `docs/testnet_commissioning.md` before claiming transaction readiness.
  Machine setup alone is insufficient: the qualification GTC/query/cancel,
  ordinary attended reduce-only close, WebSocket recovery and bounded
  response-loss injection are explicit implementation gaps.
- The router VM is network-only. It receives no API-wallet, account config,
  execution state, Keychain access, repository/shared-folder mount, approval
  secret, agent runtime or venue authority.
- Router rendering accepts public topology and key strings attested by the
  operator as WireGuard public keys; the encoding cannot prove provenance.
  Generate each private key on its owning machine and never place it in the
  repository, profile JSON, cloud-init, chat, environment or argv.
- A failed route may still produce a durable unknown submission attempt after
  authority is consumed. Preserve reservation and reconcile; never add a
  direct-network fallback or blind retry.
- Router preparation may generate only its isolated WireGuard keys on their
  owning machines. It must not run executor `init`, provision a venue/Keychain
  credential, install launchd, issue a grant or perform a harness venue write.
  Mainnet remains hard-disabled.

## Agent workflows

- Use `$operate-trading-desk` for multi-stage desk coordination.
- Use `$assess-asset` for local tracking, completed-candle TA, explicit sentiment evidence, and buy/sell/nothing/unavailable assessment.
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
