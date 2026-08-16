---
name: hyperliquid-orders
description: Place, cancel and modify Hyperliquid orders correctly from the desk computer - limit and IOC (market-style) orders, take-profit and stop-loss trigger orders with grouping, client order ids, reduce-only, batch actions, price and size rounding, and how to read every response status. Write path - Execution Trader only, on an approved ticket. Use for any order action and for reconciling by cloid.
license: MIT
metadata:
  version: "1.0.0"
  author: Galleon Labs
  category: hyperliquid
  network-default: testnet
---

# Hyperliquid orders

Everything here ends in a signed request to `/exchange`. On this desk only the Execution Trader runs it, only on a ticket with a Risk PASS and the user's approval by id, and only once per approval (`desk-execution-protocol`). Reads used for reconciliation are in `hyperliquid-account`.

## Concepts you must get right

- **Asset index, not symbol.** Perps use the index of the coin in `meta.universe` (BTC is 0 on mainnet, but never hardcode: read `meta`). Spot uses `10000 + index` in `spotMeta.universe`. The Python SDK's `Exchange` accepts the coin name and resolves the index; the TS SDK wants the number.
- **Price rounding.** At most 5 significant figures, and at most `6 - szDecimals` decimal places for perps (`8 - szDecimals` for spot). Integer prices are always valid. Wrong precision is rejected by the exchange.
- **Size rounding.** Round **down** to the market's `szDecimals`. Never round up.
- **Minimum order value** is 10 USD notional.
- **Time in force:** `Gtc` rests until filled or cancelled; `Ioc` fills what it can immediately and cancels the rest; `Alo` (add liquidity only) rests or is rejected if it would take.
- **There is no market order.** A market-style order is an `Ioc` limit at a price bounded by your slippage tolerance (buy: above mid; sell: below mid).
- **reduceOnly** orders can only reduce an existing position; use it for exits, stops and take-profits.
- **cloid** (client order id) is `0x` + 32 hex characters (16 bytes). Unique per order. It lets you query and cancel an order even if the response was lost.
- **Trigger orders** (`tp`/`sl`): `triggerPx` is the **mark price** that arms the order; `isMarket: true` executes market-style once triggered, `false` places a limit at `p`. `p` is always required and acts as the worst-acceptable price after the trigger, so for market triggers set it beyond the trigger: a sell trigger's `p` below `triggerPx`, a buy trigger's `p` above it. A stop whose `p` equals its trigger can rest unfilled through a gap, so the desk defaults to a **5% bound for stop-losses** (filling matters more than slippage) and **1% for take-profits**; the app uses 10% for both. The ticket may override.
- **Grouping**: `na` (independent orders); `normalTpsl` (entry plus TP/SL as one-cancels-other tied to that entry: children are sized to the entry, placed only when it fills, cancelled if it is cancelled, and when one child fills the sibling is cancelled); `positionTpsl` (TP/SL tied to the position rather than to an order, shown as the position's own TP/SL, `isPositionTpsl: true`). Every TP/SL with an explicit size is **fixed-size** once placed; it does not resize when the position changes. The app's "entire position" TP/SL is a reduce-only trigger sent with size `0` under `positionTpsl` grouping (such orders show up live in `frontendOpenOrders` as `sz: "0.0"`, `isPositionTpsl: true`); rehearse it on testnet before the desk relies on it.
- **Responses:** each order in an action gets a status: `{"resting": {"oid": ...}}`, `{"filled": {"totalSz", "avgPx", "oid"}}`, `"waitingForTrigger"`, `"waitingForFill"`, or `{"error": "..."}`. A top-level `{"status": "err", "response": "..."}` means the whole action was rejected.

## Python (official SDK, `hyperliquid-python-sdk`)

Common header for every snippet below (network, account, key loader, rounding helpers):

```python
import os, secrets
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

def load_key():
    k = os.environ.get("HYPERLIQUID_PRIVATE_KEY")
    if not k:
        p = os.path.expanduser("~/.hyperliquid/api-wallet.key")
        if os.path.exists(p):
            k = open(p).read().strip()
    if not k:
        raise SystemExit("no API wallet key available - see hyperliquid-setup section 4")
    return k

NETWORK = os.environ.get("HYPERLIQUID_NETWORK", "testnet")
BASE = constants.MAINNET_API_URL if NETWORK == "mainnet" else constants.TESTNET_API_URL
ACCOUNT = os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"]           # main account the API wallet acts for

info = Info(BASE, skip_ws=True)
exchange = Exchange(eth_account.Account.from_key(load_key()), BASE, account_address=ACCOUNT)

SZ_DECIMALS = {a["name"]: a["szDecimals"] for a in info.meta()["universe"]}

def round_px(coin, px, spot=False):
    """5 significant figures, then at most (6|8) - szDecimals decimals. Integers are always valid,
    so above 100,000 keep whole-dollar precision instead of rounding to tens."""
    px = float(px)
    if px >= 100_000:
        return float(round(px))
    max_dec = max((8 if spot else 6) - SZ_DECIMALS[coin], 0)
    px = float(f"{px:.5g}")
    return float(Decimal(str(px)).quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_HALF_UP))

def round_sz(coin, sz):
    """Round DOWN to szDecimals (Decimal, so 0.29 stays 0.29 and never becomes 0.28)."""
    return float(Decimal(str(sz)).quantize(Decimal(1).scaleb(-SZ_DECIMALS[coin]), rounding=ROUND_DOWN))

def new_cloid():
    return Cloid.from_str("0x" + secrets.token_hex(16))
```

### Resting limit order (Gtc)

```python
coin, is_buy, sz, px = "ETH", True, round_sz("ETH", 0.51), round_px("ETH", 3000)
assert sz * px >= 10, "below 10 USD minimum order value"
cloid = new_cloid()
print("cloid", cloid.to_raw())                     # write this to the proposal file BEFORE sending
res = exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}}, reduce_only=False, cloid=cloid)
print(res)
```

### Market-style order (IOC with a slippage bound)

```python
coin, is_buy, sz, slippage = "ETH", True, round_sz("ETH", 0.51), 0.002      # 20 bps
mid = float(info.all_mids()[coin])
px = round_px(coin, mid * (1 + slippage) if is_buy else mid * (1 - slippage))
cloid = new_cloid(); print("cloid", cloid.to_raw(), "bound px", px)
res = exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=False, cloid=cloid)
print(res)
# The SDK also offers exchange.market_open(coin, is_buy, sz, px=None, slippage=0.01, cloid=cloid): same semantics,
# it rounds the PRICE for you but not the size (pass round_sz), and its default slippage is 5% if you omit it.
# State the slippage bound in the report either way.
```

### Entry with stop-loss and take-profit in one action

```python
coin, sz = "ETH", round_sz("ETH", 0.51)
bound_tp, bound_sl = 0.01, 0.05                                   # worst-acceptable price after trigger (desk defaults)
entry, tp, sl = round_px(coin, 3000), round_px(coin, 3090), round_px(coin, 2900)
tp_px, sl_px = round_px(coin, tp * (1 - bound_tp)), round_px(coin, sl * (1 - bound_sl))   # sells: p below trigger
c_entry, c_tp, c_sl = new_cloid(), new_cloid(), new_cloid()
orders = [
  {"coin": coin, "is_buy": True,  "sz": sz, "limit_px": entry, "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False, "cloid": c_entry},
  {"coin": coin, "is_buy": False, "sz": sz, "limit_px": tp_px, "order_type": {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}}, "reduce_only": True, "cloid": c_tp},
  {"coin": coin, "is_buy": False, "sz": sz, "limit_px": sl_px, "order_type": {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}}, "reduce_only": True, "cloid": c_sl},
]
res = exchange.bulk_orders(orders, grouping="normalTpsl")   # entry + children as one-cancels-other
print(res)
```

Sell-side entries mirror this: `is_buy=False`, TP trigger below entry, SL trigger above, children `is_buy=True` with `p` **above** their triggers.

The children come back as `waitingForFill` while the entry rests; they are placed once the entry fills (fully, or partially followed by a margin cancel), cancelled if the entry is cancelled, and when one child fills the sibling is cancelled (`siblingFilledCanceled`). Cancelling a partially filled entry cancels the children too; protect the filled part with a separate stop.

### Stop-loss on an existing position

```python
coin, bound = "ETH", 0.05                        # 5% worst-acceptable bound for a stop-loss
pos = next(p["position"] for p in info.user_state(ACCOUNT)["assetPositions"] if p["position"]["coin"] == coin)
szi = float(pos["szi"])                          # positive long, negative short
sz, is_buy_close = round_sz(coin, abs(szi)), szi < 0
trigger = round_px(coin, 2900)
worst = round_px(coin, trigger * (1 + bound) if is_buy_close else trigger * (1 - bound))
res = exchange.order(coin, is_buy_close, sz, worst,
                     {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}},
                     reduce_only=True, cloid=new_cloid())
print(res)
```

This is a standalone reduce-only trigger (`grouping="na"`, as in the official SDK example) with a fixed size: after a partial fill, an add or a reduce, place a new stop for the actual size and then cancel the old one. The alternative is the app's position-tied form: the same trigger with `sz=0` submitted via `bulk_orders([...], grouping="positionTpsl")`, which closes whatever the position is when it fires (`frontendOpenOrders` shows it as `sz: "0.0"`, `isPositionTpsl: true`). Rehearse the size-0 form on testnet before using it on mainnet.

### Cancel

```python
exchange.cancel("ETH", oid)                                    # by exchange order id
exchange.cancel_by_cloid("ETH", Cloid.from_str("0x..."))       # by client order id
exchange.bulk_cancel([{"coin": "ETH", "oid": 1}, {"coin": "BTC", "oid": 2}])
# statuses: ["success"] or [{"error": "Order was never placed, already canceled, or filled."}]
```

Cancel-all-for-account does not exist as one action; list `open_orders(ACCOUNT)` and cancel each, or use the dead-man's switch (`hyperliquid-advanced`).

### Modify

```python
# Modify = cancel the resting order and place the new one in one action (the SDK sends batchModify).
# oid may be an int or a Cloid. The replacement gets a NEW oid; pass a fresh cloid and record it.
res = exchange.modify_order(oid, "ETH", True, round_sz("ETH", 0.51), round_px("ETH", 2995),
                            {"limit": {"tif": "Gtc"}}, reduce_only=False, cloid=new_cloid())
print(res)   # statuses like an order response: resting / filled / error
```

Limits that matter: without the raw `always_place` flag (which the SDK's `modify_order` never sets and the desk does not use), the replacement **must be a non-trigger order that will rest** - `Alo`, or a `Gtc` that would not execute immediately. So `modify` is for moving or resizing a resting limit order. **Stops and take-profits cannot be modified**: place the new trigger order first, confirm it is resting, then cancel the old one, so the position is never unprotected. If the original order was already filled or cancelled, the modify fails and nothing new is placed.

### Read the response

```python
if res.get("status") == "ok":
    for st in res["response"]["data"]["statuses"]:
        if "resting" in st:      print("resting oid", st["resting"]["oid"])
        elif "filled" in st:     print("filled", st["filled"]["totalSz"], "@", st["filled"]["avgPx"], "oid", st["filled"]["oid"])
        elif st in ("waitingForTrigger", "waitingForFill"): print(st)
        elif "error" in st:      print("REJECTED:", st["error"])
else:
    print("ACTION REJECTED:", res.get("response"))
```

Then reconcile: `info.query_order_by_cloid(ACCOUNT, cloid)`, `info.open_orders(ACCOUNT)`, `info.user_fills(ACCOUNT)`, `info.user_state(ACCOUNT)` (`hyperliquid-account`).

## TypeScript (`@nktkas/hyperliquid`)

```ts
import { ExchangeClient, HttpTransport, InfoClient } from "@nktkas/hyperliquid";
import { formatPrice, formatSize, SymbolConverter } from "@nktkas/hyperliquid/utils";
import { privateKeyToAccount } from "viem/accounts";
import { randomBytes } from "node:crypto";

const isTestnet = (process.env.HYPERLIQUID_NETWORK ?? "testnet") !== "mainnet";
const transport = new HttpTransport({ isTestnet });                 // network lives on the transport
const info = new InfoClient({ transport });
const wallet = privateKeyToAccount(process.env.HYPERLIQUID_PRIVATE_KEY as `0x${string}`);
const exchange = new ExchangeClient({ transport, wallet });
const conv = await SymbolConverter.create({ transport });
const a = conv.getAssetId("ETH")!, szDec = conv.getSzDecimals("ETH")!;
const cloid = ("0x" + randomBytes(16).toString("hex")) as `0x${string}`;

// resting limit
const res = await exchange.order({
  orders: [{ a, b: true, p: formatPrice("3000", szDec), s: formatSize("0.51", szDec), r: false, t: { limit: { tif: "Gtc" } }, c: cloid }],
  grouping: "na",
});
console.log(res.response.data.statuses[0]);       // { resting: { oid } } | { filled: {...} } | "waitingForFill" | "waitingForTrigger"

// entry + tp + sl grouped (p on the triggers = worst acceptable price after trigger: 1% for tp, 5% for sl)
await exchange.order({
  orders: [
    { a, b: true,  p: "3000", s: "0.51", r: false, t: { limit: { tif: "Gtc" } } },
    { a, b: false, p: "3059", s: "0.51", r: true,  t: { trigger: { isMarket: true, triggerPx: "3090", tpsl: "tp" } } },
    { a, b: false, p: "2755", s: "0.51", r: true,  t: { trigger: { isMarket: true, triggerPx: "2900", tpsl: "sl" } } },
  ],
  grouping: "normalTpsl",
});

await exchange.cancel({ cancels: [{ a, o: 123 }] });
await exchange.cancelByCloid({ cancels: [{ asset: a, cloid }] });
await exchange.modify({ oid: 123, order: { a, b: true, p: "2995", s: "0.51", r: false, t: { limit: { tif: "Gtc" } } } });
```

The TS client **throws** `ApiRequestError` when any order in the batch has an `error` status; catch it and read `error.response` to see which legs rested. `formatPrice`/`formatSize` truncate (never round up), and `formatPrice` applies the 5-significant-figure cap even above 100,000 (so `117234.5` becomes `117230`); pass an integer string yourself if you want whole-dollar precision there. The package is ESM-only and needs Node 22.12+; run snippets as `.mjs` files or with `"type": "module"` in `package.json`.

## Raw wire format (for reference and for reading responses)

```json
{"action": {"type": "order",
            "orders": [{"a": 1, "b": true, "p": "3000", "s": "0.51", "r": false,
                        "t": {"limit": {"tif": "Gtc"}}, "c": "0x9f3e...c1a2"}],
            "grouping": "na"},
 "nonce": 1723819200000, "signature": {"r": "...", "s": "...", "v": 27}, "vaultAddress": null}
```

Signing (msgpack of the action, keccak, EIP-712 with a phantom agent) is done by the SDKs. Do not hand-roll it on the desk.

## Error strings you will meet

| Response text | Meaning | Fix |
| --- | --- | --- |
| `Price must be divisible by tick size.` | price precision wrong | `round_px` (5 sig figs, `6 - szDecimals` decimals) |
| `Order must have minimum value of $10.` | notional too small | size up or REJECT the ticket |
| `Insufficient margin to place order.` | not enough free margin at this leverage | ticket back to Risk |
| `Reduce only order would increase position.` | wrong side or no position | re-read `clearinghouseState` |
| `Post only order would have immediately matched, bbo was ...` | `Alo` would cross | reprice or use `Gtc` |
| `Order could not immediately match against any resting orders.` | IOC found no liquidity inside the bound | widen bound only via a new ticket |
| `Invalid TP/SL price.` | trigger on the wrong side of mark | fix trigger direction |
| `Order price too far from oracle` | limit far outside reference | reprice |
| `Order would cause position to exceed margin tier limit at current leverage` | notional lands in a lower-leverage tier | lower leverage or size; back to Risk |
| `Order was never placed, already canceled, or filled.` (cancel) | nothing to cancel | reconcile with `orderStatus` |
| `User or API Wallet 0x... does not exist.` (top level) | signature or hash mismatch, wrong network, or wrong key | check `HYPERLIQUID_NETWORK`, key, `account_address` |
| `Must deposit before performing actions.` | account has no funds on this network | fund (testnet faucet or user deposit) |

A whole-batch rejection (empty batch, non-reduce-only TP/SL, price far from reference) comes back as one top-level `{"status":"err","response":"..."}` and applies to every order in the batch.

## Pitfalls

- Sending twice after a timeout. Query by cloid first (`desk-execution-protocol`).
- Rounding a price with more than 5 significant figures (`3000.1` for ETH is fine; `3000.15` is six figures and rejected; integers such as `63200` are always fine).
- Forgetting `reduce_only=True` on stops and take-profits; a non-reduce-only trigger can open a new position, and non-reduce-only TP/SL in a batch is rejected outright.
- Setting a market trigger's `p` equal to its trigger: it may not fill after a gap. Put `p` beyond the trigger by the ticket's bound (desk default 5% for stops, 1% for take-profits).
- TP/SL sizes are fixed once placed. After a partial fill, add or reduce, re-place protection for the actual size (place new, then cancel old).
- An `Ioc` entry with `normalTpsl` children that fills only partially leaves the filled part **unprotected**: the children are never placed (they need a full fill, or a partial fill followed by a margin cancel). Place a standalone stop for the filled size at once.
- Trigger direction: a `sl` for a long has a trigger **below** mark; a `tp` above. Reversed for shorts. Triggers fire on **mark** price, not last trade.
- Trying to `modify` a stop or take-profit, or to modify a limit into a price that would execute: both are rejected. Place-then-cancel instead.
- `Alo` orders that would cross are rejected, not converted.
- Using the API wallet's address as `account_address`. It must be the main account.
- Frontend market orders show up in reads as `orderType: "Market"`, `tif: "FrontendMarket"`; do not send those values from the API.
- Address rate limits are volume-based (see `hyperliquid-api-reference`); a fresh account has a buffer of 10,000 actions, then 1 per 1 USDC traded. Cancels get extra headroom when limited (`min(limit + 100000, 2 x limit)`), not unlimited.
