---
name: assess-asset
description: Track a Hyperliquid asset, combine completed-candle TA, registered signals, and sourced sentiment into buy, sell, nothing, or unavailable, and optionally stage a non-authoritative TESTNET learning ticket. Use for monitoring or opportunity assessment; do not use it to approve or execute a trade.
---

# Assess Asset

Use the harness result as the authority for calculations and capability state.

1. Call `get_harness_status`. Require research tools to be enabled; report the venue-write state separately.
2. Resolve the exact Hyperliquid symbol and mainnet or testnet **market-data** network. Tracking always remains `execution_environment: shadow`, uses the frozen 4h candidate, and defaults to a 60-second poll. Call `list_tracked_assets`; reuse only an active tracker whose symbol, network, query, and cadence match exactly. A paused or mismatched tracker requires explicit user direction. If absent, call `track_asset` with a stable asset ID and frozen X query, and disclose the local research-database write.
3. Use `get_market_brief` for current funding, open interest, liquidity and timestamps. It is context, not the registered signal.
4. Call `get_latest_sentiment`. If the user requests current X research or the snapshot is absent/stale, read [manual sentiment evidence](references/manual-sentiment.md), conduct only a visible user-assisted browser read, then disclose and call the second local write, `record_manual_sentiment`. Never post, like, follow, message, or run unattended website automation.
5. Call `analyze_asset`, disclosing that it appends an immutable local analysis/learning record. Preserve its two distinct results:
   - `descriptive_technical` is broader EMA/RSI/ATR context and has no validation inheritance.
   - `registered_signal` is the frozen candidate-v0 buy/sell/nothing calculation.
6. Report the harness `assessment.verdict` unchanged. `unavailable` means evidence is missing or stale; it is not `nothing`. A directional result with `eligible_for_risk_quote: false` is research, not a position recommendation.
7. If the user asks whether the rule has demonstrated edge or is profitable, call `validate_candidate_profitability`. Do not make that validation a prerequisite for a small infrastructure-learning experiment: its purpose is to gather execution evidence, and it must remain explicitly `profitability_qualified: false`.
8. If—and only if—the user asks to prepare the directional candidate for a TESTNET learning trade, call `stage_trade_candidate` with the exact saved `analysis_hash` and a stable idempotency key. Report the returned ticket, bracket, stressed loss, expiry, grant/loss evidence hashes, and every blocker unchanged. Staging creates no approval, risk reservation, credential access, signature, or order.
9. Stop at staging. Approval in chat is invalid. Tell the user that `show-stage` and `authorize-stage` belong to the separate direct-terminal `trading-harness-executor` control boundary; never invoke or emulate that confirmation through an agent tool.

Return source and receipt times, registered signal/reason, sentiment method and quality, descriptive TA, optional profitability status, stop/target geometry when present, the learning cycle or staging document ID, and the exact blocker to the next stage. Never invent confidence, size outside the deterministic quote, treat browser evidence as unattended authority, or imply an order was sent.
