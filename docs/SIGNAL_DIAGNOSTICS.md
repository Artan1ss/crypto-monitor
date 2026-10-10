# Signal diagnostics and observational outcomes

The original V1 scoring weights, thresholds, divergence threshold (0.5), pivot
parameters, ATR stop buffer, target multiples and cooldown are unchanged.
Event-time input corrections can change which signals qualify. Structure filtering
and countertrend warnings are display context only, never additional score filters.

## Upgrade and restart

Stop the current Uvicorn process, run `python -m alembic upgrade head`, then restart it.
Migration `0002_signal_diagnostics` only adds nullable evidence columns and result
fields; it preserves every existing decision and feature value.

Existing minutes have `is_complete=NULL`: their capture quality was never recorded.
They do not count toward fresh continuous warmup. A new deployment needs approximately
one hour of complete captured data at the default warmup setting. Missing minutes,
disconnections and partial restart minutes invalidate CVD comparisons that span them.
Historical candles cannot reconstruct missing trades.

## Exact decision boundaries

* `signal_time` remains the signal candle START for backwards compatibility.
* `created_at` is the actual decision generation time; new Telegram messages label this
  separately from candle start. `telegram_sent_at` records delivery acknowledgement.
* A candle at 19:45 UTC uses trades in `[19:45, 19:50)`, CVD from the 19:49 minute
  close, and no 19:50 partial-minute input.
* A pivot at 18:35 uses the 18:39 minute close. This definition is candle-close CVD,
  not CVD at the intrabar high/low: the latter requires retained raw trade timestamps.
* The normalizer uses the median absolute delta of complete previous 5m buckets,
  excludes the signal candle and all later data, and has the existing 20-bar maximum.
  At least `min(20, CVD_WARMUP_BARS)` valid prior buckets are required.
* Near-zero normalizers are diagnosed and the unreliable input is rejected; no new
  tunable divergence threshold or arbitrary dollar floor was introduced.
* Independent streams are allowed up to five seconds to cross the candle-end watermark.
  Evaluation runs in a separate worker so waiting never blocks trade ingestion.
  Missing watermarks skip evaluation rather than fabricate data.
* OI and funding snapshots are selected at/before the candle close, with a recorded
  15-second freshness check. Hourly input includes only candles closed by that boundary.

Flow minutes record closure quality. First startup/reconnect minutes are partial;
next fully observed minutes can become complete. Duplicate trade IDs are ignored within
a bounded 100,000-ID in-memory window; IDs are not persisted across restarts. Late
previous-minute messages invalidate affected minutes instead of moving CVD backwards.
Restarts within a minute retain already persisted minute totals. Graceful shutdown
flushes partial totals, but a hard crash can lose unpersisted trades; completeness
checks protect comparisons crossing that interval rather than backfilling CVD.

## Immutable live debug snapshot

Every inserted signal stores `signals.debug_snapshot` JSONB before Telegram delivery.
It contains schema and implementation versions, source SHA256, original parameter
values (excluding database URL, Telegram token and chat ID), exact generation/close
times and latency, all captured 5m/1h candle input history, raw flow rows, ticker inputs,
pivot/sweep geometry, candidate structure levels and breach times, order-book metrics,
stream status/watermarks, ingestion counters, score breakdown, levels and warnings.

Each market's CVD diagnostics include exact reference/current timestamps and values,
raw and directional difference, independently summed deltas between closes, per-minute
continuity errors, actual signal-candle delta, each baseline bucket and its delta,
excluded buckets, raw/effective denominator, epsilon usage, threshold, strength,
missing/partial minutes and data-validity reasons. Decimal values are JSON strings
so calculation precision is not lost.

Snapshots describe captured aggregate inputs, not an archive of every raw trade.
They are never overwritten by subsequent outcome updates. Large normalized strength
means a multi-bar CVD difference divided by a typical single-bar delta; it is neither
a z-score nor a probability and naturally depends on pivot age.

`structure_target` now considers only opposing pivots not touched/breached since their
pivot candle. Snapshot risk context also records structure distance in R. No minimum
structure-R trading filter has been added.

## Automatic result tracking

A separate worker updates `signals.outcomes` every ten seconds and resumes after
restart by querying persisted signals. All signals are tracked, including Telegram
disabled/failed signals. Horizons are 15, 30, 60 and 120 minutes from generation time.

Each horizon records MFE/MAE in USDT and R, observed first touches of stop and TP1/2/3,
touch time intervals, target/stop order (including ambiguity), horizon-end sampled
price/return, observation count, missing bars, boundary sampling gaps and exit age.
The tracker waits five additional minutes after the last horizon before freezing
results so finalized bars have time to arrive. Elapsed-horizon completion is separate
from data coverage: a completed result may still have incomplete historical evidence.

Only finalized 5m candles wholly inside the post-generation interval are used for
OHLC extremes. The signal candle and any boundary-straddling candle are excluded,
because they contain prices before generation or after the horizon. Five-second ticker
samples cover boundary intervals and missing-bar intervals when available. Boundary
extremes are lower bounds and between-sample touches may be missed. Gaps over 15
seconds are explicitly flagged; samples spaced less than that still aren't tick data.
When stop and a target occur in one OHLC candle, ordering is ambiguous, not a win.
Observed touches after an observed stop are retained as market-path facts, not profits.

Optional `TRACKING_FEE_BPS_PER_SIDE` and `TRACKING_SLIPPAGE_BPS_PER_SIDE` default to
zero and affect only hypothetical horizon-end net returns. Nonzero values are charged
on entry plus exit notionals. They never affect signals or claim actual executions.
Do not compute a headline win rate from ambiguous/incomplete observations.

## Inspect and reconstruct

* `GET /signals?limit=20`: IDs, generation times, decisions and tracking status.
* `GET /signals/{id}/debug`: complete stored diagnostic snapshot.
* `GET /signals/{id}/outcomes`: automatically updated horizon results.
* `python -m btc_monitor.audit`: read-only mathematical reconstruction of legacy signals.
* `python -m btc_monitor.audit --persist`: fill missing legacy snapshots and results.
* `--refresh-reconstruction`: explicitly refresh only historical reconstructions;
  live snapshots remain immutable.

Legacy snapshots are labelled `historical_reconstruction`, include the original
features, and declare that original parameters/capture completeness/live partial
trade state are unknown. Mathematical reconstruction does not relabel past signals.

## Verification

The 2026-10-09 19:45 UTC real-signal fixture reproduces the recorded spot strength
`9.4921535165` under the legacy next-minute-partial and inclusive-pivot-reference
assumption. The closed-window reconstruction instead gives:

* Pivot close CVD at 18:39: `-5092854.71256054` USDT.
* Signal close CVD at 19:49: `-2897612.68751124` USDT.
* Difference, also verified by summing 70 minute deltas: `2195242.02504930` USDT.
* Previous-20-bar median absolute delta: `224668.18249660` USDT.
* Corrected mathematical strength: `9.7710410110`; epsilon was not used.
* Signal-candle spot delta: `+2453.90924930` USDT, not the saved `-24490.01069180`.

The legacy spot reference used 18:40 CVD (one minute late), adding `61755.25635160`
to the reference value; an inferred 19:50 partial delta of `-901.89015860` accounts
for the remaining difference. This exactly matches the saved strength, but the
partial was inferred rather than archived. The large strength mainly reflects a
14-bar cumulative difference divided by a typical one-bar delta, not an epsilon
explosion. Arithmetic consistency does not prove that no trades were missed by the
legacy feed; legacy capture completeness remains explicitly unknown.

Run `python -m pytest -q`. Opt-in PostgreSQL validation also exercises duplicate signal
protection, JSONB persistence, minute completeness and result reconstruction through
a recreated repository. All synthetic writes use a transaction and roll back:

```powershell
$env:RUN_POSTGRES_TESTS = "1"
python -m pytest tests/test_postgres.py -q
Remove-Item Env:\RUN_POSTGRES_TESTS
```
