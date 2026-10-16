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
# 路径复盘与评分 v2

本次调整只影响独立 1H 模块的证据展示/追踪，不改变触发阈值、冷却、方向或 V1 的 0–8 分策略。

## 发出时证据置信分

`h1-evidence-score-v1` 为可解释、未经胜率校准的 0–100 规则分：价格拒绝/回踩确认
25，摆动点背离20（仅滚动背离12），现货压力15，现货源方向一致性15，成交量10，
合约背景10，OI/费率背景5。现货压力项按 `min(1, 同向压力/(2×现有压力阈值))` 线性计分；
一致性按同向现货源比例；量比从1倍到1.5倍线性增加；合约同向或其压力绝对值不及现货
同向压力一半得背景10分；OI增加且报告费率偏反方向得背景5分，不能据此推断多空持仓。
缺少必要方向/现货压力时不评分。分数不参与触发或过滤，不是成功概率，也不是仓位建议。

新信号在原始快照内冻结分数、版本、分项，仅使用原始决策输入，Telegram 同时显示。
旧信号仅从原快照派生分数，标记 `historical_snapshot_rescore`，不回写原快照，不补发通知。

## 方向与结构独立

1/4/12/24H 各保存：

- **方向结果**：即使途中有缺口，只要目标窗口的最后一个完整分钟收盘存在，仍可计算
  终点变动。没有终点分钟不能用更早价格冒充。生成时间有秒数时显式保留终点边界秒差；
  这不是精确到秒的收益。
- **方向分**：`clip(50+25×终点方向R,0,100)`，0R为50，+2R为100，-2R为0。
  未到期时是进行中评分，到期后才是窗口方向结果；缺少终点则留空。
- **结构分**：可证明先到1R为100，先到失效为0，到期完整路径均未到为50；同分钟双触及、
  首次事件之前缺口或无法排除起始分钟触及时留空。后来的大跌不能推翻已经先失效的事实。
- **分钟覆盖分**：合格完整分钟数/预期完整分钟数×100，只衡量覆盖率，不是置信概率。
- **极值下界**：已有分钟内的最大有利/不利美元波动，未扣交易成本；缺口或起始非完整分钟
  的极值可能更大。不是交易盈亏。

不再用 `unknown_due_to_gaps` 统一否决所有结果：缺口在首次已知触及之后不影响“谁先到”
的证明；缺口在之前则保留 `unresolved_missing_prefix`，同时展示已观察次序与事件时间，
不冒充完整证明。生成时间不是整分钟时，如该分钟高低价均未触及1R/失效，可排除起始
分钟风险；否则为 `unresolved_partial_start_minute`。同分钟双触及仍不猜测先后。

## 补采、重试与审计

只用 Bybit BTCUSDT linear 的完整1分钟 OHLC 复盘，支持 `live` 和官方 `rest_history`。
官方完整历史K线优先作为价格证据，能够修复直连逐笔流的不完整分钟；不会修复 CVD，
不替换成其他交易所，也不把不存在的分钟补零。记录未来可用时间、异常OHLC、错误交易对
均不纳入。每个窗口展示具体缺失分钟、排除原因、原始输入ID和历史价格输入ID。

在线追踪每轮最多20条（按最早更新时间公平排序）；每条最多查询其24H窗口，最多50000条
输入，超过上限报错而不静默截断。自动补采全局至多每5分钟一次，每次最多200分钟、15秒
超时，同一信号每6小时最多一次；受403等访问限制时记录类型/状态，不记录响应体或凭据。
最近7天内已到期但不完整的结果每6小时可重新计算；旧版本结果（包括原来已结束的）也会
自动升级。原始快照不改写，前后结果保存在 `hourly_outcome_revisions`，相同语义结果去重。
复算不会触发 Telegram，部署须先执行 `python -m alembic upgrade head`。

手动复算最多500条最早更新的记录（可重复执行推进批次），不会发送通知：

```powershell
python -m btc_monitor.hourly.cli retrack
# 可选：允许按以上限额尝试公开历史价格补采
python -m btc_monitor.hourly.cli retrack --repair
```

单条导出现在同时包含快照、最新结果、最近200个修订与它们使用的价格/质量原始输入；
完整修订保留在数据库，不因导出上限删除。

本地也可用导出原始路径验证新算法，不写云端、不发通知：

```powershell
python -m btc_monitor.hourly.evidence --review review.json.gz --path-evidence path.json.gz --output verified.json.gz
# 可选：本地读取官方公开历史价格，最多8个有界请求/信号；证据只保存在离线文件
python -m btc_monitor.hourly.evidence --review review.json.gz --path-evidence path.json.gz --repair-prices --output repaired.json.gz
```

路径文件应含 `signal`、`rows`（完整原始记录）和 `queried_at`，原快照/参考价必须与复盘
文件相同。补采记录保留在 `local_price_evidence`，使用由内容指纹生成的负数本地证据ID，
不能误认为云端数据库ID。输出注明验证信号范围及本地验证状态，其他信号结果不伪装为已复算。
