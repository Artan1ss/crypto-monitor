# Optional Coinalyze research integration

Coinalyze complements the public exchange-direct archive. It never calls the V1
strategy, changes its thresholds/normalizer, sends Telegram alerts or places orders.
No extra database migration is required beyond `0003_market_archive`.

## Safe configuration

Add your existing key directly to the local `.env`, or later to Railway variables:

```dotenv
COINALYZE_API_KEY=your-key-here
COINALYZE_ENABLED=false
COINALYZE_POLL_SECONDS=120
COINALYZE_REQUESTS_PER_MINUTE=25
COINALYZE_SPOT_VOLUME_UNIT=unknown
COINALYZE_FUNDING_UNIT=percent
```

Do not send the key in chat, screenshots, URLs or Git commits. `.env` is gitignored.
The key is a Pydantic `SecretStr` excluded from config dumps/repr. The client only
sends it in an authentication header to the fixed official HTTPS origin, refuses
redirects and does not expose response bodies/request headers in errors. HTTPX and
HTTPcore debug logging are disabled for this client. No private exchange API is used.

Having a key configured does **not** start collection. To enable the optional source
alongside the normal app, set both `ARCHIVE_ENABLED=true` and `COINALYZE_ENABLED=true`,
then restart. A process started before these variables were set will not change itself.
One-minute provider history is fetched with a five-minute overlap approximately every
120 seconds; identical rows are deduplicated. Discovery/reference checks run at startup
and on a daily refresh. Gaps longer than the overlap need explicit historical backfill.

```powershell
# Auth/metadata/unit checks and small historical reads, no database writes:
python -m btc_monitor.archive.cli coinalyze-probe
# Explicit local historical job (sample UTC dates; edit as needed):
python -m btc_monitor.archive.cli coinalyze-backfill --start 2026-10-11T17:00:00Z --end 2026-10-11T18:00:00Z --interval 1min
```

Supported provider intervals are `1min`, `5min`, `1hour`, `4hour`, `daily`.
Ranges are UTC-aligned to the selected interval, closed, explicit and at most 31 days.
Each historical request covers at most 1,000 intervals. Longer studies require separate
jobs and actual provider retention; pagination cannot recover data the provider deleted.

## Metadata-driven coverage

The integration joins `/exchanges`, `/spot-markets` and `/future-markets`. Exchange
suffixes are not guessed. Selection matches the configured archive universe exactly:
BTC base, supported exchange symbols/quotes, and stable-margined perpetuals only.
Ambiguous or missing markets are reported rather than silently expanding/shrinking scope.

The live check on October 11, 2026 found Bybit spot/perpetual, Binance spot/perpetual,
OKX perpetual and Coinbase BTC-USD spot: six matches. OKX BTC-USDT spot was not matched
in the Coinalyze directory. Its existing direct WebSocket remains enabled independently.
Future coverage can change; the runtime reports the actual discovered list.

Stored datasets use distinct names:

- `provider_flow`: OHLCV, buy/sell volume and native delta, with BTC delta only when normalized.
- `provider_oi`: original OI OHLC, reported close and unit/basis diagnostics.
- `provider_funding`: original funding OHLC, native percent and fractional close.
- `provider_liquidations`: observed long/short liquidation volume and units.
- `provider_metadata`: live discovery/reference-check diagnostics.

Every record preserves `provider=coinalyze`, the provider symbol, sanitized market
metadata, whitelisted original bar fields, interval, receipt time and database
availability time. Provider records are append-only and distinct from direct feeds.

## Unit and timestamp safeguards

Futures volume/OI/liquidation units come from the market's denomination field.
Base-asset quantities are BTC; quote-asset/contract quantities stay native. They are
not converted to BTC by dividing through an arbitrary candle close.

The spot schema does not declare volume units. With the default `unknown`, startup
compares five older closed minutes with the direct exchange's historical BTC volumes.
At least three same-timestamp, positive-volume matches are required, all within
one part per million. Binance's taker-buy field is additionally checked. A mismatch
or missing direct reference leaves BTC normalization unavailable. The October 11
live check matched all five minutes for each of the three supported spot markets.
This checks scale/time alignment; it is not proof of exhaustive trade delivery.

Explicit spot overrides `base` or `quote` are available only for an independently
verified setup. Do not use them just to make incomplete coverage look green.

The official current Funding schema uses **percent**, not fractional exchange rates:
`0.001%` becomes fraction `0.00001`. The default funding-history conversion follows
that rate convention. Raw values remain preserved. Provider rates can differ from
exchange snapshots because of update timing and reported-vs-predicted/settled semantics;
the code does not force equality or infer a funding payment interval from candle length.
Funding overrides `fraction`/`unknown` are available for diagnostic investigation.

Historical `t` is an interval-start timestamp in **seconds**. The current endpoint's
`update` is milliseconds, so it must not be treated as a history timestamp. Historical
OI/funding closes are dated as usable at interval **end**, preventing future close
values from leaking into an earlier decision. Original interval-start fields remain intact.

OI's BTC unit alone does not establish its single-/double-sided counting basis.
Provider OI is therefore not silently substituted into the direct OI aggregate;
within-provider changes retain the same symbol and unit and are labeled independently.

## Rate limits and health

The official allowance is 40 calls/minute/key, with each requested symbol consuming
a call even in a batch. This client uses a conservative default 25-symbol-cost/minute
rolling limiter (maximum configurable 30), counts retries, and honors `Retry-After`
before retrying a 429. Authentication errors do not repeatedly retry within one request;
the background source backs off to at least five minutes. Errors do not kill V1.

The limiter is per client/process. Do not run backfills/probes and live collection
simultaneously with the same key, or share the key with other jobs without a budget.
The server's shared-key limit still applies and can delay a job. All selected perpetual
markets receive flow/OI/funding/liquidation requests; spot markets receive flow only.

`/archive/health` includes a `coinalyze` section: success time, safe error, selected and
missing feeds, calibration, per-series returned counts and request/symbol costs.
Transport freshness (`healthy`) is separate from full requested coverage
(`coverage_complete`). Known missing markets/sparse series are not hidden.

## Replay and limitations

Existing export/replay commands include these observations. See
[MARKET_ARCHIVE.md](MARKET_ARCHIVE.md) for hashed datasets and `as_received` vs `historical`.
Replay's `providers.coinalyze` section exposes per-feed flow, native/BTC delta,
provider OI changes, funding and observed short liquidations. It selects one closed
bar per timestamp/resolution; it never sums duplicate downloads or mixes resolutions.
Coarse bars are usable only in matching aligned windows, not synthetic minute bars.

Provider and direct aggregates stay separate to avoid counting the same exchange twice.
Provider aggregate BTC delta is null when a required venue or unit is missing; observed
partial sums and complete-feed lists are labeled separately. Thus the six matched feeds
can support diagnostics without pretending they include the absent OKX spot series.

Liquidation responses are often sparse. A missing bar is not assumed to mean zero;
observed volumes are reported separately from a complete-window total. Empty coverage
must not be used as evidence that no squeeze/liquidation happened.

Intraday history retains roughly 1,500–2,000 points per interval; daily retention is
longer. Persist useful intraday data continuously before it expires. This integration
still does not implement squeeze scoring, advance graded notifications or trading PnL.

Official sources: [API documentation](https://api.coinalyze.net/v1/doc/) and
[OpenAPI schemas](https://api.coinalyze.net/v1/doc/api-spec.json).
