---
name: scan-signals
description: Prepare or interpret read-only scans for exact, registered deterministic signal definitions. Use for requested market scans, SMA matches, watch conditions, or signal checks; do not use it to improvise indicators or submit trades.
---

# Scan Signals

1. Call `get_harness_status` and require research tools. Resolve the asset with `list_tracked_assets`; use `$assess-asset`/`track_asset` first if it is not registered.
2. Resolve the exact thesis, rule, code, data, venue, network, session, and timeframe versions. A parameter override is a new `draft` thesis and can produce only an exploratory observation.
3. Require completed observations, source and receipt timestamps, freshness, sequence/gap state, immutable hashes, the observed values, and earliest actionable time.
4. Call `analyze_asset` for the deterministic registered signal. Use `get_market_brief` only for separately labelled current context; it cannot calculate or verify the signal.
5. Interpret output as `unavailable`, `nothing`, `buy`, or `sell`, and report quote/staging eligibility separately. Direction without profitability qualification is research evidence; a user-requested TESTNET infrastructure-learning stage must be handed to `$assess-asset`, never treated as an order.
6. Return the rule and thesis versions, evidence status, observed values, timestamps, data quality, validation summary, invalidation condition, and `no_trade: true`.

Do not invent a side or position size, call `validate_trade_intent`, request approval, or trigger execution. Missing, stale, partial, cross-network, or disputed data produces `unavailable`, not a directional inference.
