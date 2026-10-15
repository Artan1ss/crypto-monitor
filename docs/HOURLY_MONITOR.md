# BTC 1H Pressure Monitor / Research Desk

This is a separate experimental monitor (`h1-pressure-v1.1`). V1's original Bybit
5-minute inputs, scores, thresholds, Telegram rendering and tracking are unchanged.
No orders are placed. Notifications describe evidence, not guaranteed future moves.

## Detection contract

Only closed UTC hours are evaluated, 180 seconds after hour end by default. Data must
have actually become available by that fixed decision deadline. Late downloads do
not repair a past decision or fabricate an earlier alert. Saved feature decisions are
immutable; a separate evaluated flag permits recovery if alert persistence is interrupted.

The default Coinalyze cohort is fixed: Bybit, Binance, Coinbase spot; Bybit, Binance,
OKX stable-margined perpetuals. OKX spot is explicitly excluded, not silently counted
as zero. `HOURLY_SOURCE=direct` uses all seven public streams instead. Sources and
cohorts are never mixed or dynamically shrunk to hide gaps. This is not CoinGlass's
entire-market aggregate and will not exactly reproduce the user's screenshot.

Minimum context: 25 consecutive price hours of the same cohort, with complete flow
and verified BTC units throughout the current six-hour CVD window. Older flow gaps
do not break valid ATR/price structure; any pivot-to-now CVD comparison crossing a
gap is unavailable, never filled with zero. Missing price/current flow blocks detection.
Missing OI/funding remains null and is
reported as unavailable, not interpreted as neutral or zero.

- WATCH: six-hour price change opposes sufficiently strong spot delta/volume pressure,
  or price retests an already confirmed swing with opposing spot CVD since that swing.
  Rolling/pivot watches require at least two spot venues agreeing over the six-hour window.
- CONFIRMED: a known level is exceeded but the hour closes back inside by 0.05 ATR,
  or price approaches within 0.25 ATR and produces a rejecting body/wick (0.2 ATR).
  It must also have rolling/pivot divergence or supporting current-hour spot pressure.
  A price-only wick without flow evidence is not a confirmed signal.
- Both bearish and bullish patterns are supported. Two left candles and one right candle
  confirm pivots; the right candle must end **before** the trigger hour. A pivot's CVD
  reference is its hour close, not the unknown intrahour instant of its price high/low.
- Default material spot pressure is 5% of actual BTC turnover, not a tiny rolling delta
  normalizer. The denominator, numerator, per-venue votes and alignment timestamps
  are all retained. Ratios outside [-1,1] and zero volume are invalid.
- ATR is the mean of the prior/current 14 closed true ranges (not V1's Wilder ATR).
  Minimum opposing six-hour price movement is 0.15 ATR. These are explicit experimental
  starting rules, not calibrated profitability parameters.
- OI growth and opposing-sign reported funding are context, not proof of short/long
  positions. Funding is an **unweighted reported-rate mean**, not OI-weighted or
  payment-interval-normalized. Provider OI counting basis remains unverified.
- Contract volume is compared with the median of the previous 20 hours in BTC units.
  High volume is supporting context, not a mandatory gate or dollar-volume aggregate.

### Notification levels and delivery

All candidates persist, including cooldown-suppressed candidates. Telegram delivery
has a four-hour cooldown per direction and tier; a WATCH can upgrade to CONFIRMED
without waiting for the WATCH cooldown. This does not mean a confirmed position exists.

Live messages use a recent Bybit ticker sample (at most 30 seconds old), not a stale
hourly close disguised as an executable entry. Missing fresh price or a price already
beyond invalidation suppresses delivery and remains visible in the ledger.

Newly launched workers first reconstruct retained historical features without sending
past messages. Historical records are marked `retrospective` in charts and statistics;
only subsequent newly closed hours can create live messages. Backfill is never described
as a notification that was actually sent. All retrospective inputs still use original
availability deadlines; no future-data fitting or screenshot-derived synthetic bars.

The database outbox claims a durable `attempted` state before HTTP transmission.
Acknowledged sends become `sent`; network uncertainty becomes `uncertain`. Automatic
resends are deliberately disabled after an ambiguous attempt because Telegram offers
no idempotency key. A crash after transmission can therefore leave `attempted`; it is
not falsely marked delivered. Pending messages older than ten minutes are suppressed.
Delivery also requires a fresh ticker and a price still inside the invalidation level;
otherwise the record is retained with an explicit suppression reason, without a TG send.
Monitor errors and delivery states are visible; this is not an exactly-once guarantee.

## Evidence and outcomes

`0004_hourly_monitor` adds `hourly_bars` and `hourly_alerts`; it never rewrites V1 tables.
Each signal preserves rule version, code hash, all rule parameters, trigger flags,
25-hour feature context, every used raw observation ID, per-hour lineage hashes,
normalization metadata, pivot confirmation/alignment times, generation/reference times
and origin. Raw `market_observations` remain append-only with no automatic deletion.
The complete per-signal export includes these immutable raw records; missing references
return an explicit error instead of an incomplete “full” export. Retain the archive
along with signal records in database backups.

1H/4H/12H/24H outcomes begin at actual live generation (not the trigger hour's start).
They record signed closing-price change, observed MFE/MAE lower bounds, hypothetical
1R/2R and invalidation touches, per-window coverage and first-touch intervals. Only
full Bybit linear minutes after generation are used. The two minute boundaries and
intraminute order are unknown; two simultaneous touches are `ambiguous_same_minute`.
Gaps make the verdict `insufficient_data`. Direction is supported/opposed only with
complete minute-interior coverage and a fresh endpoint, outside a 0.05% flat band.
This is not simulated execution or realized profitability; no fees or fills are claimed.
The dashboard does not label partial or gapped paths as completed horizon returns.

## Configuration / private access

```dotenv
HOURLY_ENABLED=true
HOURLY_TELEGRAM_ENABLED=true
HOURLY_SOURCE=coinalyze
HOURLY_DECISION_DELAY_SECONDS=180
HOURLY_CONTEXT_HOURS=48
HOURLY_FLOW_HOURS=6
HOURLY_PRESSURE_THRESHOLD=0.05
HOURLY_PRICE_MOVE_ATR=0.15
HOURLY_LEVEL_TOLERANCE_ATR=0.25
HOURLY_COOLDOWN_HOURS=4
```

Existing archive/Coinalyze and Telegram collection must be enabled separately. This
worker reads the database; it makes no additional Coinalyze API requests. Keep one
replica and serverless off. An independent PostgreSQL session advisory lock prevents
two 1H workers from emitting overlapping notifications. It is distinct from archive
and migration locks. Requests, bootstrap, manual replay and notification sends are serialized.

Open `http://127.0.0.1:8000/dashboard` locally. The dashboard has aligned OHLC/volume,
spot/futures CVD, OI and funding plots, signal markers, direction/tier/origin filters,
clickable diagnostics/outcomes and complete-snapshot downloads. CVD resets to zero
at the beginning of the visible uninterrupted segment; gaps are not interpolated.
Candles with missing aggregate flow remain visually subdued. Marker arrows are
generated-at timestamps; small close dots identify the triggering hour's end.

Each CVD panel now checks **its own** market coverage, not the overall hour flag.
A spot-unit failure cannot hide complete futures CVD. Genuine gaps are shaded and
the next uninterrupted segment is explicitly marked as a new cumulative baseline.
Do not compare absolute CVD heights across such a reset.

Dashboard/API chart bars and CLI review exports may include a read-only unit recovery
overlay for old incomplete hours. This uses the original raw minutes plus strict unit
proofs that were already available **before each observation was received** and valid
within 48 hours. Future evidence, missing minutes and explicit quote/contract units
cannot be used as substitutes. `review_recovery` retains the original completeness and
lineage; `unit_recovery` lists every recovered row and its evidence metadata ID.
Charts label these hours as review corrections. Database feature decisions, existing
signal snapshots/outcomes and prior notification history are never overwritten, and
this read-only repair never sends messages. Old offline files need a new cloud export
to receive this overlay; the browser never invents missing raw inputs.

For UI-only local use, run `python -m btc_monitor.hourly.preview` and open
`http://127.0.0.1:8011/dashboard`. This deliberately starts no V1 collectors or
Telegram background worker; offline cloud review files can be imported directly.

Without `DASHBOARD_PASSWORD`, all data/dashboard/docs endpoints are accessible only
from actual loopback peers. Forwarded headers cannot bypass this. `/health`, `/ready`
and the non-sensitive service root remain available to Railway checks. Existing
Railway networking stays private unless the user explicitly authorizes a public domain.
For deliberate public access use a long random `DASHBOARD_PASSWORD`, username `monitor`,
and HTTPS only. Never put passwords in Git, screenshots, links or query strings.
The UI ships local assets only, with no external chart CDN or third-party analytics.

### Replay and offline export

```powershell
python -m btc_monitor.hourly.cli replay --hours 72
python -m btc_monitor.hourly.cli export --hours 72 --output .research-data/h1-review.json
python -m btc_monitor.hourly.cli export --hours 72 --output .research-data/h1-review.json.gz
```

Replay can also be started by the dashboard (authenticated same-origin JSON POST).
It returns a job status and never sends historical messages. Existing immutable
hourly decisions are reused, not revised into better-looking hindsight signals.
Exports refuse to overwrite files; the parent directory must exist. Range: 30–168H.
Replay also loads the preceding 24H baseline and can re-evaluate cached feature hours
with this additional decision-time context. It never changes pinned input bars or
existing signal snapshots, and uniqueness prevents duplicate signal records.
The CLI file includes chart features and complete signal feature snapshots, not every
raw minute. The `.gz` suffix enables lossless gzip compression; the dashboard accepts
either JSON or gzip and enforces a 50 MiB limit before and during decompression.
Individual signal HTTP exports additionally contain all raw inputs.
Use “导入离线复盘” for a file exported from the cloud: it is explicitly offline, stops
live refresh, and never sends messages or writes to the backend. Downloading a selected
offline signal exports the imported snapshot only; it cannot magically retrieve cloud
raw IDs from a different local database. Do not claim an offline file proves live health.

## Verification and next evaluation

Unit tests cover pressure divergence, symmetric long signals, failed/rejected highs,
future pivot exclusion, zero/small-denominator safeguards, same-cohort coverage,
late provider revisions, stale OI/funding, outcome ambiguity/gaps, generation boundaries,
delivery uncertainty, CSRF/route bounds and access protection. Opt-in PostgreSQL tests
verify immutable decisions, signal persistence and unique-key behavior through restart.

Evaluate October 13–14 against actually retained cloud data. Label generated historical
signals as retrospective; compare which decision-time inputs were available and whether
any signals were suppressed by gaps. A visually obvious CVD trend alone does not prove
rules would have alerted before the move. Report false positives, missed cases, alert
latency and data coverage alongside directional outcome counts. Do not tune on one case.

Primary references: [Coinalyze API](https://api.coinalyze.net/v1/doc/) and
[HTTP authentication / HTTPS requirement](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Authentication).
