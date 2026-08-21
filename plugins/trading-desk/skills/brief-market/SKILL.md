---
name: brief-market
description: Produce or interpret a sourced, freshness-aware market brief for a named instrument. Use for current price, mark, funding, open interest, volume, spread, depth, range, or liquidity questions; do not use it to generate signals or orders.
---

# Brief Market

1. Call `get_harness_status` and confirm that the requested venue, network, and read-only market-data interface are available.
2. Call `get_market_brief` with the smallest explicit scope matching the request. Do not silently substitute another instrument, venue, network, session, or timeframe.
3. Treat the tool result as the source of harness-grade live facts. If the tool is absent, disabled, stale, gapped, or internally inconsistent, return `unavailable` with the reason. Do not recreate the call with shell commands, an SDK, arbitrary request bodies, screenshots, or remembered prices.
4. Preserve source timestamps, receipt time, network, units, and freshness. For depth, state the side, distance from mid, available size, observation time, and whether the requested size exceeds visible liquidity.
5. Separate the response into `facts`, `derived`, `interpretation`, and `unknowns`. Show formulas for conversions such as open-interest notional or funding annualization.

A brief is descriptive evidence. It does not validate a thesis, identify an opportunity, choose side or size, approve an intent, or authorize execution. General web research may add clearly labelled context, but it cannot replace unavailable harness data.
