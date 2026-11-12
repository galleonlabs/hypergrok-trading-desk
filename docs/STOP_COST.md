# What a stop really costs on Hyperliquid

A stop is a trigger, not a fill. When it fires it becomes a market order: a long's stop sells into the bids below the trigger, a short's buys from the asks above it, and the exit pays the taker fee. Size a trade from the distance you drew on the chart and every stop that fires loses more than you budgeted, always in the same direction.

`scripts/stop_cost.py` shows the gap for one trade, from the live Hyperliquid order book. It needs no key, wallet or account: you type your equity, and it reads only the public `/info` endpoint.

## Run it

From the root of a checkout, with Python 3's standard library:

```bash
python3 scripts/stop_cost.py --coin ETH --entry 3000 --stop 2900 --equity 10200 --risk-pct 0.5
```

```
HYPERGROK STOP COST · ETH-PERP · LONG
READ ONLY · MAINNET · 2026-10-14T08:48:15.592000Z
Sources: Hyperliquid /info metaAndAssetCtxs + l2Book

Plan        entry 3,000.00 · stop 2,900.00 · risk 0.5% of $10,200.00 = $51.00
Book now    mark 2,504.73 · spread 0.4 bps
            0.5100 ETH sold into the bids: 0.2 bps from mid
Stop fill   2,897.00 = 2,900.00 - 3.00 slippage (10.0 bps of entry, the desk floor)
Fees        2.65 per ETH = (3,000.00 + 2,897.00) x 0.045% taker, both legs
Distance    drawn 100.00 · stressed 105.65 per ETH

Sized on              size   stop costs   of equity
Drawn stop      0.5100 ETH       $53.88      0.528%   $2.88 over budget (+5.6%)
Stressed stop   0.4827 ETH       $51.00      0.500%   within budget
```

Those are the numbers on ticket `HG-20260816-01` in the [README](../README.md#a-day-on-the-desk). Sized on the drawn distance of 100 per ETH, the position is 0.51 ETH and a triggered stop costs $53.88, which is 0.528% of equity against a 0.5% budget. Sized on the stressed distance of 105.65 per ETH, it is 0.4827 ETH and the same stop costs the $51.00 that was budgeted.

The stop is below the entry, so the trade is a long; put the stop above the entry for a short. The entry is your plan, not the current price, and the script prints the live mark beside it.

## How it prices the stop

It follows the desk's sizing arithmetic in [`desk-risk-limits`](../skills/desk-risk-limits/SKILL.md) section 2.2:

```
risk_usd          = equity x risk_pct
slip              = entry x slip_bps / 10,000
stop_fill         = stop - slip (long)   |   stop + slip (short)
fees_per_unit     = (entry + stop_fill) x taker_fee      (entry leg and exit leg)
stressed_distance = |entry - stop_fill| + fees_per_unit
size              = round_down(risk_usd / stressed_distance, szDecimals)
```

`slip_bps` is the largest of three numbers, and the output names the one that applied:

+ a floor, 10 bps of the entry price by default, the desk's convention (`--slip-bps` changes it; `--slip-bps 0` leaves the other two);
+ the current spread, the minimum section 2.2 allows;
+ the live book's impact: the drawn-stop size walked through the side a triggered stop hits, measured from the current mid. When twenty levels at full precision do not hold that size, the book is re-read at a coarser price resolution, which moves a fill by up to one bucket, and the output names the resolution. When no page holds it, the impact is a floor rather than a total and the output says the budget is unproven.

Fees are charged at the taker rate on both legs, as the desk does. The exit of a triggered stop always pays taker; the entry is charged taker too, so the budget still holds if a limit entry crosses the book. The rate defaults to 0.045%, Hyperliquid's base perp tier. Volume tiers, staking and referral discounts lower it; pass your own rate with `--taker-fee`. HIP-3 markets such as `xyz:TSLA` set their own fees, so the script requires `--taker-fee` for them.

The stressed distance is a sizing input, not a promise. A gap through the stop, a thin book or a liquidation cascade can cost more than it assumes. The script also flags a size under Hyperliquid's $10 minimum order value or under one lot, where the desk would reject the ticket.

## For scripts and agents

```bash
python3 scripts/stop_cost.py --schema
python3 scripts/stop_cost.py --coin BTC --entry 110000 --stop 112000 --equity 25000 --risk-pct 1 --json
```

`--schema` describes the JSON without touching the network. `--json` returns decimal strings and the same versioned error contract as the Opening Bell: exit 0 for a result, 1 when market data is unavailable (`source_unavailable`) or the live book leaves no positive stop fill (`unpriceable_stop`), and 2 for invalid arguments. `--testnet` reads the testnet book, which is thinner than mainnet's.

The output is arithmetic on public data. It is not a trading signal, it does not read or change an account, and it never sends an order. On the desk, the Risk Manager runs the same arithmetic from your live account before a ticket reaches you.
