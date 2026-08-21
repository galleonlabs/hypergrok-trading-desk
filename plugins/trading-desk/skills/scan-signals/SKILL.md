---
name: scan-signals
description: Prepare or interpret read-only scans for exact, registered deterministic signal definitions. Use for requested market scans, SMA matches, watch conditions, or signal checks; do not use it to improvise indicators or submit trades.
---

# Scan Signals

1. Call `get_harness_status` and require an available deterministic scanner, registered rule version, and trustworthy normalized data. If any is missing, return `unavailable`; do not substitute an ad hoc calculation.
2. Resolve the exact thesis, rule, code, data, venue, network, session, and timeframe versions. A parameter override is a new `draft` thesis and can produce only an exploratory observation.
3. Require completed observations, source and receipt timestamps, freshness, sequence/gap state, immutable hashes, the observed values, and earliest actionable time.
4. Use `get_market_brief` only for separately labelled current context. A market brief cannot calculate or verify a registered signal.
5. Interpret deterministic scanner output using only these statuses: `unavailable`, `observation`, `research_candidate`, or `validated_research_signal`. The last requires independently validated evidence.
6. Return the rule and thesis versions, evidence status, observed values, timestamps, data quality, validation summary, invalidation condition, and `no_trade: true`.

Do not call a match an opportunity, infer a side or position size, call `validate_trade_intent`, request approval, or trigger execution. Missing, stale, partial, cross-network, or disputed data produces no directional inference.
