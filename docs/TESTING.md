# Testing

## Deterministic suite

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest -q --cov=hypergrok --cov-fail-under=75
python -m build
```

The suite uses fake SDK modules and API responses. It proves the only send carries the exact builder object and cloid, and that malformed plans, wrong confirmations, network mismatches, account mismatches, builder failures, duplicates, stale prices and unauthorised signing wallets all produce zero sends. Journal tests prove one plan hash can reserve the local send boundary only once and that terminal/unknown records cannot return to sending.

## Live read-only smoke

```bash
python scripts/live_smoke.py
```

This calls public read endpoints only:

+ Hyperliquid testnet `allMids`
+ Hyperliquid mainnet `allMids`
+ DefiLlama protocol data for Hyperliquid
+ CoinGecko data for Hyperliquid

It imports no signing module and submits no action. A green smoke test proves connectivity and basic response shape, not trading readiness.

## What is never automated in CI

No order, cancellation, transfer, approval, deposit, withdrawal, bridge or reward claim. Mainnet and testnet execution are verified through the same fake one-send contract and fail-closed gates.
