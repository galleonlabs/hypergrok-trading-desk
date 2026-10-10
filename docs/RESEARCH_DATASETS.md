# Research datasets

`scripts/research_dataset.py` saves public Hyperliquid perp candle history and optional hourly funding as a replayable research bundle. It requires Python 3's standard library, no SDK, wallet, account address or key. It calls only public `/info` and writes to a new local directory. No dataset result authorizes a trade.

## Fetch a bounded period

From the repository checkout:

```bash
python3 scripts/research_dataset.py --schema
python3 scripts/research_dataset.py \
  --coin ETH --interval 4h \
  --start 2026-09-01T00:00:00Z --end 2026-10-01T00:00:00Z \
  --with-funding \
  --output /workspace/trading-desk/data/eth-september --json
```

The example requests September 2026, not a rolling window. Choose dates for the actual research question. Both boundaries must be explicit UTC times aligned to the candle interval. The range is **[start, end)**: start is included and end is excluded. End cannot be later than the time observed when collection starts; an open candle is never a finished observation. With `--with-funding`, both boundaries must also fall on whole UTC hours.

Supported intervals are `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `8h`, `12h` and `1d`. The tool intentionally omits the exchange's longer calendar/session intervals; it does not guess their alignment. At most 5,000 candle slots may be requested. This bounds work, but does not guarantee availability: the API retains only its latest 5,000 candles for an interval. An older or newly listed market can leave gaps even in a smaller request. Pagination cannot recover candles outside that retention window.

The output directory must not already exist, including for a retry. Use a fresh name and retain the first attempt's evidence. A failed read is not a reason to replace the original requested period with whatever happened to arrive.

## Market and source identity

The tool resolves perp names through exchange metadata and retains canonical spelling, including mixed-case names. Select a HIP-3 market with a prefixed coin such as `--coin xyz:TSLA`, or with a coin and explicit `--dex xyz`. It does not search every DEX for a plausible match, mix markets or support spot pairs.

Unlike a live execution snapshot, historical research may include a market flagged as delisted in current metadata. That flag travels with the bundle; it is not permission to trade it. Current metadata is not a historical universe or listing calendar, so it cannot by itself remove survivorship bias or prove availability before a market launched.

Mainnet is the default. `--testnet` selects the official testnet API; a different explicit `--base-url` is identified as `custom`. Carry the recorded origin, network, DEX and canonical coin into the research report. A custom origin's response and matching hashes do not establish that Hyperliquid supplied it.

## What the bundle contains

| File | Purpose |
| --- | --- |
| `sources.json` | Captured request bodies, parsed API responses and retrieval times, including metadata and history pages; sanitized failures when a read fails |
| `candles.csv` | Validated, sorted observations for the requested interval; original decimal values are preserved |
| `funding.csv` | Present when funding was requested; observed hourly rates and premiums with their actual millisecond timestamps |
| `manifest.json` | Request and source identity, SHA-256 file hashes, coverage counts and missing ranges, duplicate accounting, errors and overall result |

No row is invented to fill a gap. Missing funding is never zero funding. Identical duplicate observations are removed and counted; conflicting observations with the same identity make the result unavailable instead of silently choosing one.

Funding records can arrive milliseconds after a UTC hour boundary. The CSV retains that actual timestamp, and coverage uses its hour bucket. Use the actual timestamp when deciding whether an observation was available to a strategy; bucketing is only a coverage check. The collector follows timestamp-overlap pagination, validates repeated observations and stops on exhausted or non-progressing history. It paces requests by at least three seconds and does not automatically retry failed network reads. The default funding budget is 100 requests; `--max-pages` accepts 1 through 250. Exhausting the budget on a full page is incomplete even if observed counts match: collection has not established that pagination finished.

## Verify before analysis

```bash
python3 scripts/research_dataset.py \
  --verify /workspace/trading-desk/data/eth-september --json
```

`--schema` and `--verify` are offline. Verification checks the saved file hashes and replays captured responses to reproduce normalized files and coverage. A manifest edited to claim completeness must agree with that replay. The verifier does not refresh data or contact the exchange.

| Result | Meaning | Exit code |
| --- | --- | --- |
| `complete` | Every requested candle slot and, when requested, funding hour is covered by validated observations | `0` |
| `incomplete` | The requested coverage is missing observations or collection exhausted its page budget | `1` |
| `unavailable` | A source or validation failure prevents a trustworthy requested dataset | `1` |
| Invalid arguments or failed verification | The requested operation did not pass | `2` for arguments; `1` for verification |

An internally consistent incomplete bundle remains incomplete after verification. Check the process exit code and reported result; the presence of files is not success. A failure before a bundle can be written may return only a diagnostic, so retain that output too.

Hashes and replay verify **internal consistency**, not independent provenance. Someone able to rewrite all files can manufacture a consistent bundle. Preserve the manifest's hash in a separate run record if later accidental changes need to be detectable, and keep source trust separate from local integrity.

## Analyst to Strategist handoff

The Market Analyst supplies the bundle directory, manifest hash, requested range, canonical market, source/network, collection times, coverage result, gap ranges and offline verification result. In the strategy folder's `data.md`, the Strategist records those details before using the CSVs. Each run cites that same immutable bundle and its verification result.

If coverage is incomplete or unavailable, do not run the originally specified experiment as if the period were complete. Report what is missing. If the user agrees to a narrower experiment, write a new data boundary into the rules before evaluating results and collect a separate bundle. Do not quietly crop the sample, forward-fill prices or assign zero to absent funding.

`complete` is a data-coverage result, not a strategy PASS or Risk PASS. Candles do not reconstruct intrabar execution, spreads, historical liquidity or the historical oracle series. Funding cash flow depends on position size and oracle price at payment time: rates and candle closes alone cannot prove exact USD funding PnL. Record any price proxy and sensitivity analysis as assumptions. Keep costs, holdout use, trial counts, rejected experiments and the desk's normal ticket approval and execution boundaries.

## Sources checked 2026-10-10

- [Hyperliquid info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint): candle availability, field identity, supported intervals and timestamp-overlap pagination.
- [Hyperliquid perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals): DEX metadata, delisting flags and funding history records.
- [Hyperliquid funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding): hourly payment and oracle-based cash flow.
- [Hyperliquid rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits): weighted public-read budget. Local pacing is a client bound, not a guarantee against HTTP 429 on a shared IP.

Fixed UTC windows, strict validation, local request limits, replay verification and handoff requirements are this tool's research controls; they are not exchange guarantees.
