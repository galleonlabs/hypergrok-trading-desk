# Every-DEX account read

Hyperliquid runs builder-deployed (HIP-3) perp DEXs, such as `xyz`, beside the main one. Each DEX has its own clearinghouse, so a plain `clearinghouseState` read shows only the main DEX: HIP-3 positions and the margin behind them are missing, and equity comes out too low.

`scripts/account_read.py` reads one account across the main DEX, every HIP-3 DEX and spot, then reports positions and per-DEX and total equity. It uses Python 3's standard library and the public `/info` endpoint. It needs the account address, never a key, and it signs nothing.

## Run it

From the root of a checkout, pass the account (main wallet) address, not an API wallet's address:

```bash
python3 scripts/account_read.py --address 0xYOUR_ACCOUNT_ADDRESS
```

Or set it once for the shell, so it stays out of command history and shared transcripts:

```bash
export HYPERGROK_ACCOUNT=0xYOUR_ACCOUNT_ADDRESS
python3 scripts/account_read.py
```

Output from the synthetic test account, a standard-mode account with a short on `xyz` and margin on a USDH-collateral DEX:

```
HYPERGROK ACCOUNT READ · 0x1111...1111 · DEFAULT MODE
READ ONLY · CUSTOM · 2024-01-01T00:00:00Z
Sources: Hyperliquid /info perpDexs, userAbstraction, clearinghouseState x4, spotClearinghouseState, spotMetaAndAssetCtxs, meta

DEX    collateral   account value         USD   positions
main   USDC              1,000.00   $1,000.00           1
xyz    USDC                500.00     $500.00           1
flx    USDH                200.00     $199.80           0
1 other DEX empty.

Positions
  ETH  long 0.1 @ 3,000.00 · value 300.10 · uPnL +0.10 · cross 20x · liq none
  xyz:TSLA  short 2.0 @ 250.00 · value 490.00 · uPnL +10.00 · isolated 10x · liq 300.00

Spot        $180.00 across 2 balances
Equity      perps $1,699.80 + spot $180.00 = $1,879.80
```

## What it reads

1. `perpDexs` lists the DEXs: `null` for the main one, then each builder DEX by name.
2. `userAbstraction` gives the account mode, which decides whether a total can be summed.
3. `clearinghouseState` once per DEX, with `"dex": "<name>"` for each builder DEX. Every DEX is read, so a new one appears without a code change.
4. `spotClearinghouseState` for spot balances, and `spotMetaAndAssetCtxs` for token names and prices.
5. `meta` with the DEX name, only for a DEX that holds a balance or position, to find its `collateralToken`.

A failed or malformed read fails the whole command (exit 1, `source_unavailable`). It never prints a partial total that silently leaves a DEX out. About fifteen requests make one read, well inside the public rate limit.

## How equity is computed

Hyperliquid's [account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes) decide how balances add up.

In the standard mode (`default`, also `disabled`), spot and each perp DEX keep separate balances. The script sums:

```
dex_usd    = marginSummary.accountValue x USD price of the DEX's collateral token
spot_usd   = sum of balance total x USD price of the token (total includes amounts on hold)
total_usd  = sum of dex_usd over every DEX + spot_usd
```

`accountValue` already includes unrealized PnL and isolated margin, so positions are not added again. USDC counts as exactly $1. Any other token, including a DEX collateral such as USDH or USDT0, is priced at the mid of its USDC spot pair, or the mark when the book has no mid. A nonzero balance with no USDC pair is listed as unpriced and left out; the total is then marked incomplete and printed as "at least".

In the `unifiedAccount` and `portfolioMargin` modes, one balance backs spot and perps, and Hyperliquid documents that `spotClearinghouseState` then holds all balances and that per-DEX user states are not meaningful. Adding them would count collateral twice, so the script reports the positions, per-DEX states and spot value with `total_usd` null. It does the same for any other mode, including the discontinued DEX abstraction.

Not included: staked HYPE, vault deposits and sub-accounts, which have their own reads in `hyperliquid-account`. Reads are sequential, so prices can move between them.

## Comparing with the Hyperliquid app

The total is designed to match the app's equity for a standard-mode account read at the same moment. That match has not been checked against the app; the arithmetic and request shapes were checked against the official [perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) and [spot](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot) references and live public responses. Small differences can come from spot prices (the app may value a token differently from its USDC pair mid), from illiquid tokens whose mid value cannot be realized, and from the seconds between reads. Treat a large gap as a reason to compare the per-DEX rows.

## For scripts and agents

```bash
python3 scripts/account_read.py --schema
python3 scripts/account_read.py --json
```

`--schema` describes the JSON without touching the network. `--json` returns decimal strings in the same versioned contract as the other read-only tools: exit 0 for a result, 1 when a read fails (`source_unavailable`) and 2 for invalid arguments, including a missing or malformed address. `--testnet` reads the testnet account, which is a different account from mainnet's at the same address.

The read is a fact source for sizing and reconciliation, not authority to act. The desk's Risk PASS, the user's exact ticket approval and the single writer still govern every exchange action, and this script never sends one.
