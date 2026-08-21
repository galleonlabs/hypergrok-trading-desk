---
name: validate-thesis
description: Structure or review a trading thesis with frozen rules, complete trial accounting, holdout discipline, and prospective validation. Use for strategy ideas, indicators, imported setups, backtests, or claims of trading edge; do not use it to authorize or execute trades.
---

# Validate Thesis

Keep scientific evidence separate from permission to trade.

1. Call `get_harness_status` to discover whether a thesis registry, evidence store, or deterministic validator is actually available. If not, produce a validation plan or review supplied artifacts only; do not claim that a test ran or evidence was persisted.
2. Freeze the instrument and point-in-time universe, venue and tradable proxy, data source and bar/session construction, exact feature and signal formula, observability time, direction, entry, exit, stop, expiry, sizing rule, costs, primary metric, economically useful minimum effect, attempted parameter family, stopping rule, and holdout boundary.
3. Undefined terms keep the thesis at `draft`. Any material rule, data, cost-model, or parameter change creates a new version.
4. Record every attempted and inherited variant. Once inspected, a holdout is permanently discovery data. Correct for the complete selection family or require fresh prospective evidence when that family is unknown.
5. Require next-observable execution, conservative fees/spread/slippage/funding, relevant baselines and placebos, parameter-neighborhood tests, regime and instrument stability, effect size with uncertainty, and a prospective shadow period.
6. Report the evidence state, missing or failed gates, costs, uncertainty, family size and correction, holdout/shadow status, and reproduction hashes. End with `no_trade: true`.

Agents may explain evidence but may not mark their own work validated or create a deployment grant. `validate_trade_intent` validates an intent boundary; it is not a thesis validator and must not be used as one.
