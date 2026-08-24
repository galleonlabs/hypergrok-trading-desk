---
name: test-strategy
description: Design, run, or review leakage-resistant evaluations of a fully specified trading strategy. Use for backtests, out-of-sample tests, robustness checks, or comparisons; do not use it for live signal scanning or execution.
---

# Test Strategy

1. Call `get_harness_status`. For the installed candidate-v0 rule, resolve a tracked 4h asset and call `validate_candidate_profitability`; for any other strategy, return a reproducible preregistration/test plan unless a matching runner exists.
2. Require a frozen thesis version before outcomes are inspected. Register the primary metric, minimum useful effect, parameter family, multiplicity correction, sample-size plan, stopping rule, and untouched holdout.
3. Use immutable point-in-time data with source and content hashes, completed observations, explicit calendars and bar alignment, delistings and symbol changes, and no forward-filled signal prices. `get_market_brief` is current context, not a historical test dataset.
4. Simulate only next-observable decisions and include spread, fees, slippage, funding or borrow, latency, liquidity, rejected orders, and capacity assumptions appropriate to the strategy.
5. Separate discovery, selection, untouched holdout, and prospective shadow periods. Compare with simple baselines, matched random rules, nearby parameters, delayed execution, and removal of dominant trades.
6. Report every attempted variant, trade and exposure counts, net effect with uncertainty, drawdown and tail outcomes, turnover and costs, concentration, parameter/regime stability, and reproduction hashes. Keep in-sample and out-of-sample results separate.

A favorable test updates evidence only through an independent governed process. It does not create an executable signal, validate a trade intent, issue a deployment grant, or authorize capital. Return `no_trade: true`.
