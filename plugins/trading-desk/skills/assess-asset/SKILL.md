---
name: assess-asset
description: Track a Hyperliquid asset and combine completed-candle TA, registered signals, sourced sentiment, and profitability evidence into buy, sell, nothing, or unavailable. Use for asset monitoring or opportunity assessment; do not use it to approve or execute a trade.
---

# Assess Asset

Use the harness result as the authority for calculations and capability state.

1. Call `get_harness_status`. Require research tools to be enabled; report the venue-write state separately.
2. Resolve the exact Hyperliquid symbol and mainnet or testnet **market-data** network. Tracking always remains `execution_environment: shadow`, uses the frozen 4h candidate, and defaults to a 60-second poll. Call `list_tracked_assets`; reuse only an active tracker whose symbol, network, query, and cadence match exactly. A paused or mismatched tracker requires explicit user direction. If absent, call `track_asset` with a stable asset ID and frozen X query, and disclose the local research-database write.
3. Use `get_market_brief` for current funding, open interest, liquidity and timestamps. It is context, not the registered signal.
4. Call `get_latest_sentiment`. If the user requests current X research or the snapshot is absent/stale, read [manual sentiment evidence](references/manual-sentiment.md), conduct only a visible user-assisted browser read, then disclose and call the second local write, `record_manual_sentiment`. Never post, like, follow, message, or run unattended website automation.
5. Call `analyze_asset`. Preserve its two distinct results:
   - `descriptive_technical` is broader EMA/RSI/ATR context and has no validation inheritance.
   - `registered_signal` is the frozen candidate-v0 buy/sell/nothing calculation.
6. Report the harness `assessment.verdict` unchanged. `unavailable` means evidence is missing or stale; it is not `nothing`. A directional result with `eligible_for_risk_quote: false` is research, not a position recommendation.
7. For any profitability or “should we trade this?” claim, call `validate_candidate_profitability`. Historical PASS is insufficient by itself: prospective shadow, sentiment-increment, drift, independent review, account/risk, approval, and execution gates remain separate.
8. Stop after reporting the research result. Do not call `validate_trade_intent` or improvise a continuation into execution, even when directional. A future execution workflow must be a separately qualified skill and typed tool surface.

Return source and receipt times, registered signal/reason, sentiment method and quality, descriptive TA, profitability status, stop/target research geometry when present, and the exact blocker to the next stage. Never invent confidence, size a position outside the deterministic risk tool, treat browser evidence as unattended authority, or imply an order was sent.
