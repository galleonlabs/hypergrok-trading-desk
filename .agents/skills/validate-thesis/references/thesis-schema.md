# Thesis Evidence Record

Required identity:

- `thesis_id` and immutable `version`
- source and registration timestamp
- code/config/data hashes
- author and independent reviewer

Frozen hypothesis:

- instrument, venue, tradable proxy, universe version
- timezone, session, exchange calendar, bar alignment, input price field
- feature and signal formula, equality/touch semantics, warm-up
- direction, earliest actionable time, entry, exit, stop, expiry
- size rule and portfolio interaction
- fees, spread, slippage, funding, borrow, latency, capacity

Experiment:

- discovery interval
- selection method and every attempted variation
- primary metric and economically useful minimum effect
- multiplicity family and correction method
- sample-size/power plan and stopping rule
- untouched holdout and prospective-shadow boundaries
- baselines, placebos, neighboring parameters, stress cases

Allowed evidence states:

```text
draft
registered
exploratory_tested
holdout_passed
shadow_confirmed
validated
rejected
inconclusive
suspended
retired
```

Evidence status never grants exchange authority. A separate human-governed, environment/account-scoped deployment grant is required.

Return:

- evidence state and version
- missing or failed gates
- net effect with uncertainty and costs
- attempted-family count and correction
- holdout/shadow status
- reproduction artifact hashes
- `no_trade: true`
