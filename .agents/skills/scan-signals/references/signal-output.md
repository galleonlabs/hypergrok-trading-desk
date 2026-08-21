# Signal Scan Output

Return one record per match:

```text
scan_id
as_of
source_id
source_timestamp
received_at
data_hash
code_hash
rule_id
rule_version
thesis_id
evidence_status
symbol
venue
network
session
timeframe
event
observed_values
observed_at
earliest_actionable_at
completed_observation
freshness
gap_state
validation_summary
invalidation
status
no_trade
```

Rules:

- `no_trade` is always `true` at the skill boundary.
- `status=validated_research_signal` requires `evidence_status=validated`.
- Any parameter override forces `status=observation` and creates a new draft thesis.
- Missing, stale, cross-network, partial, or disputed data returns `status=unavailable` and no directional inference.
