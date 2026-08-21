---
name: validate-thesis
description: Structure and review a trading thesis with frozen rules, complete trial accounting, holdout discipline, and prospective validation. Use for strategy ideas, indicators, SMA configurations, backtests, or claims of trading edge; do not use it to persist evidence, authorize, or execute trades.
---

# Validate Thesis

Keep scientific evidence separate from permission to trade.

1. Read the thesis-validation requirements in [`docs/trading_harness_spec.md`](../../../docs/trading_harness_spec.md), sections 6 and 8.
2. Require exact instruments/proxies, data and bar construction, signal calculation, direction, observability, entry, exit, stop, costs, primary metric, search family, stopping rule, and holdout boundary.
3. If a material term is undefined, return a `draft` thesis and list the missing fields. Do not invent definitions from charts or narrative.
4. Record every attempted variant. Once any holdout is inspected, mark it burned and never reuse it as untouched evidence.
5. Treat inherited selections and social-media examples as discovery evidence unless the complete selection funnel can be reconstructed.
6. The current foundation has no persistent thesis registry or backtest runner. Produce a validation plan or review supplied deterministic artifacts only. Do not claim a test ran when the corresponding project command does not exist.
7. When deterministic validators are added, use only those project interfaces. Agents may explain results but may not persist evidence, set `evidence_status=validated`, or create a deployment grant.
8. Report costs, uncertainty, multiplicity correction, parameter-neighborhood stability, concentration, and prospective-shadow status.
9. Never call an indicator match an opportunity, infer institutional causation from price geometry, or produce an order or position size.

Use the output fields and stopping conditions in [`references/thesis-schema.md`](references/thesis-schema.md).
