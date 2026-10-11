# Multi-exchange BTC research archive

This is a separate, opt-in data layer. Existing Bybit V1 signals, scoring, thresholds,
CVD normalizer, debug snapshots, outcome tracking and Telegram notifications are unchanged.
It prepares auditable datasets for later squeeze-strategy research; it is not yet a
profitable-trading backtest, squeeze predictor, liquidation collector or alert engine.

## Scope and units

Seven public feeds are supported without exchange accounts or API keys:

- Bybit: BTCUSDT spot and USDT linear perpetual.
- Binance: BTCUSDT spot and USDT linear perpetual.
- OKX: BTC-USDT spot and BTC-USDT-SWAP linear perpetual.
- Coinbase Exchange: BTC-USD spot.

This is a selected-venue aggregate, **not the entire market**. Inverse contracts,
other quote currencies, other venues, options and ETFs are not included. Public
feeds can drop trades; `complete` means no detected problem in the subscribed feed,
not proof that the exchange delivered every trade. Coinbase heartbeats and contiguous
Binance/Coinbase trade IDs help detect gaps; Bybit/OKX do not provide the same guarantee.

Live trades become event-time one-minute bars with taker buy/sell BTC, native-quote
notional, OHLC, record count, first/last trade times, delivery lag and quality reasons.
Binance aggregate-trade records are not individual fill counts. Coinbase `side` is
the maker side and is inverted. OKX swap quantities are contracts and are converted
using validated live contract metadata; a contract is not assumed to equal one BTC.

Cross-venue CVD uses **BTC quantities**, so USD and USDT notionals are never silently
summed as the same currency. Each window's CVD is the sum of its minute deltas, not
a restart-sensitive absolute cumulative level. Native-quote deltas remain available.

Each perpetual market has approximately one-minute OI/funding snapshots. OI uses
single-sided BTC quantities, not USD values that change just because price changes.
Bybit's current `singleOpenInterest` is preferred; documented both-sided
`openInterest` is divided by two in this archive only. This does not modify V1 OI.
Original provider values, basis, funding kind, interval and settlement times remain
in payloads. A current funding estimate is not interchangeable with settled funding.
When a historical funding interval is unavailable, it stays unknown; historical
rates are not normalized using today's interval.

## Storage and quality

Migration `0003_market_archive` adds `market_observations` without changing existing
V1 records. Observations include source, instrument, dataset, interval, event/end time,
receipt time, database availability time, session, quality, fingerprint and payload.
Historical REST payloads and derivative snapshots retain original provider fields.
Individual live trade packets are **not retained**; the durable evidence is minute
aggregates and quality observations. Tick-level replay is therefore not supported.

Writes are append-only and fingerprint-idempotent. Corrections/backfills add evidence
instead of overwriting earlier observations. Replay selects one row per instrument
and minute, so duplicate sessions or history/live overlap do not double-count.

- Bars finalize after a default 10-second grace period.
- Startup, reconnects, detected missing IDs, late trades, invalid timestamps and empty
  minutes are marked partial/gap rather than represented as trustworthy zero flow.
- A late trade invalidating an already-finalized minute adds a separate quality row.
- Queue/bucket/ID caches are bounded. Storage retries preserve the same batch; graceful
  shutdown also flushes rows waiting for queue capacity and marks unfinished minutes partial.
- A PostgreSQL session advisory lock allows only one live archive writer per database,
  including overlapping deployments. Backfill jobs may still append history.
- Loss of the writer-lock connection or a terminated worker stops archive workers.
  `/archive/health` becomes unhealthy; it requires operational attention/restart.

There is no local write-ahead log. A hard crash, host outage or exhausted queue can
lose unpersisted minutes. Missing coverage must be treated as missing, not reconstructed
from candle direction. No automatic retention deletion is enabled: monitor database
growth and take backups before deciding a retention policy. At the default cadence,
seven minute feeds plus six OI/funding datasets produce roughly 18,720 rows/day before
quality records, retries with revised data or historical jobs; actual disk usage varies.

`available_at` uses the later of receipt time and the database clock sampled during
insertion. This conservatively accounts for most queue delays but is **not an exact
transaction-commit timestamp**; long transactions can introduce additional latency.

## Run locally (PowerShell)

Do not overwrite an existing `.env`. Preserve Telegram/database secrets there.

```powershell
python -m pip install -e ".[dev]"
docker compose up -d postgres
python -m alembic upgrade head
python -m btc_monitor.archive.cli probe --seconds 15
```

`probe` needs no database, Telegram or keys. It checks seven public WebSockets and
perpetual metadata/OI/funding. Regional endpoint restrictions are reported as errors,
not bypassed. The separate live historical tests also verify spot REST endpoints.

Either enable collection alongside the existing app by setting `ARCHIVE_ENABLED=true`
in `.env` and restarting the app, **or** run a collector-only process:

```powershell
python -m btc_monitor.archive.cli collect --seconds 180
# Omit --seconds (or use 0) for continuous collector-only operation.
python -m btc_monitor.archive.cli coverage
```

Collector-only mode never starts V1 or sends Telegram messages. Do not intentionally
run both collectors against the same database; the second waits for the writer lock.
Keep one production app replica. Collection is disabled by default and tests do not
enable it in your running Railway service.

Settings:

```dotenv
ARCHIVE_ENABLED=false
ARCHIVE_VENUES=["bybit","binance","okx","coinbase"]
ARCHIVE_GRACE_SECONDS=10
ARCHIVE_POLL_SECONDS=60
ARCHIVE_QUEUE_SIZE=5000
```

URL overrides are in `.env.example`. In particular, Binance futures aggregate trades
use the current `/market/ws` path, not the retired generic `/ws` path.

The app exposes `/archive/health` (connectivity, freshness, tasks, backlog, storage
errors) and `/archive/coverage` (row counts and first/last event per series). V1
`/health` intentionally remains separate. Coverage counts are not a completeness
certificate; replay checks every required minute. Keep these unauthenticated APIs
private, as with the existing debug APIs. A V1-green Railway check does not prove
the independent archive is healthy.

## Historical backfill

Jobs require explicit UTC minute-aligned ranges, at most 31 days, and never include
an open candle. Replace the sample dates with the desired available range:

```powershell
python -m btc_monitor.archive.cli backfill --venue binance --market all --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z
python -m btc_monitor.archive.cli backfill --venue bybit --market all --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z
python -m btc_monitor.archive.cli backfill --venue okx --market all --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z
python -m btc_monitor.archive.cli backfill --venue coinbase --market spot --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z
```

Binance historical klines contain taker-buy volume, permitting genuine historical
minute delta. Other integrated candle endpoints do not: Bybit/OKX/Coinbase historical
backfills provide price/volume only, **not fabricated CVD**. Consequently a full
seven-feed CVD replay needs live archival coverage from deployment onward. Free
provider history also has limits (for example Binance OI's recent-history window).
OI/funding backfills retain original timestamps; unavailable data stays missing.
Results report returned/inserted rows; do not assume a requested range was filled.

Coinalyze is now an optional, independent keyed source for historical taker-flow,
OI, funding and liquidation evidence. See [COINALYZE.md](COINALYZE.md) for safe setup,
unit checks, current coverage and limitations. It does not replace or double-count
exchange-direct data; replay exposes it under `providers.coinalyze`. Registration
and secrets stay with you. No trading/withdrawal credentials are needed.

## Export and feature replay

```powershell
python -m btc_monitor.archive.cli export --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z --output .research-data/btc-day.jsonl
python -m btc_monitor.archive.cli replay --input .research-data/btc-day.jsonl --output .research-data/btc-day-1h.jsonl --start 2026-10-10T00:00:00Z --end 2026-10-11T00:00:00Z --timeframe 1h --mode as_received
```

Exports include a ten-minute baseline lookback and a companion manifest containing
expected instruments, range, row count and SHA-256. Replay verifies the hash/count.
Exports/reports refuse to overwrite existing files. `.research-data/` is gitignored;
back it up separately if needed. Input is bounded to 256 MiB, each replay to seven
days; use smaller explicit exports for longer studies.

Timeframes `5m`, `1h`, `4h`, `1d` use closed, UTC-aligned windows. The default decision
time is window end plus 15 seconds (configurable). Modes have different meanings:

- `as_received`: only evidence with `available_at <= decision_at` can be used.
  Later-downloaded history cannot retroactively make an earlier alert look possible.
- `historical`: uses subsequently obtained evidence to examine past conditions.
  Outputs explicitly say `causal=false`; this is ex-post research, not proof of early warning.

Outputs contain per-venue coverage, BTC delta, aggregate delta, venue breadth, price
OHLC, OI changes, funding age and observation lineage. The expected universe comes
from the export manifest, not whichever venues happen to have data. Aggregate delta
is null when any required flow minute is missing. Observed partial delta is separately
labeled. Detected gaps invalidate live price/flow; complete REST history may repair
the affected window only when permitted by the selected replay mode.

Coinalyze's provider series remain separate. Verified one-hour/four-hour/daily flow
bars can serve matching replay windows without being expanded into fake minute bars.
Provider OI/funding candle closes become usable at the **end** of their interval.
Missing provider venues, uncertain units and sparse liquidation responses are explicit.

`spot_buy_futures_sell` is only a descriptive opposite-flow feature. It does not
identify short positions, prove a squeeze or forecast one. This phase intentionally
does not implement new scoring, graded alerts, entry/exit rules, PnL, costs or a
strategy performance report. Those require labeled event samples and out-of-sample
validation after sufficient live history has accumulated.

## Verification

```powershell
python -m pytest
python -m ruff check src tests migrations
$env:RUN_LIVE_ARCHIVE_TESTS='1'
python -m pytest tests/test_archive_live.py -v
# Additionally enable the 150-second real seven-feed collector test:
$env:RUN_LIVE_CAPTURE_TEST='1'
python -m pytest tests/test_archive_live.py -v
# PostgreSQL tests are separate, opt-in, and roll back their writes:
$env:RUN_POSTGRES_TESTS='1'
python -m pytest tests/test_postgres.py -v
```

Network checks use public data and an in-memory store, not production writes.
PostgreSQL tests require migration 0003 first; stop any local live archive collector
before testing exclusive writer-lock acquisition. A successful offline migration SQL
check does not replace a live database test. For Docker WSL integration trouble,
do not delete distributions or reset volumes just to start this project; preserve
the PostgreSQL data and resolve Docker Engine availability first.

## Official protocol references

- [Bybit public trades](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)
  and [ticker/OI fields](https://bybit-exchange.github.io/docs/v5/market/tickers).
- [Binance futures WebSocket migration notice](https://developers.binance.com/zh-CN/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice).
- [OKX API documentation](https://www.okx.com/docs-v5/).
- [Coinbase channels, maker-side matches and drop detection](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels).
- [Coinalyze API documentation](https://api.coinalyze.net/v1/doc/).
