---
name: scan-signals
description: Prepare or interpret read-only scans for registered deterministic signal definitions with freshness and validation evidence. Use for requested market scans, SMA matches, or signal checks; the current foundation must return unavailable because no market-data adapter or scanner exists.
---

# Scan Signals

This skill is read-only.

1. Resolve the exact registered thesis/rule version before scanning. Custom parameters create a new `draft` thesis and force exploratory output.
2. The current foundation has no market-data adapter or scanner. Return `unavailable` and identify the missing deterministic interface. Do not substitute an ad hoc calculation.
3. When a scanner is implemented, use only the project's deterministic scanner and normalized market-data interface. Do not calculate a live signal from an ad hoc model narrative.
4. Require matching venue/network, completed observations, source and receipt timestamps, sequence/gap state, configured freshness, and immutable data/config/code hashes.
5. If the scanner or trustworthy data adapter is unavailable, stale, gapped, or inconsistent, stop and report `unavailable`. Do not substitute web quotes, screenshots, memory, or another network.
6. Report the exact observed values, rule version, earliest actionable time, data quality, validation evidence, invalidation, and freshness.
7. Allowed statuses are `unavailable`, `observation`, `research_candidate`, and `validated_research_signal`. Even a validated research signal is not an order authorization.
8. Never output broker/exchange actions, position size, approval language, or causal claims about institutional coordination.

Follow [`references/signal-output.md`](references/signal-output.md).
