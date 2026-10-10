# BTC Signal Monitor V1

An always-on, deterministic BTCUSDT market monitor for Bybit. It combines confirmed
liquidity sweeps, spot/futures CVD divergence, open-interest context, funding, volume,
and 1-hour trend context, then persists and optionally sends qualified signals to Telegram.

V1 is analysis-only. It never authenticates with Bybit and cannot place orders.

## What it does

- Uses independent Bybit public Spot and Linear WebSocket connections.
- Aggregates taker trades into persistent one-minute USDT-notional flow bars.
- Restores cumulative spot and futures CVD after restarts.
- Processes only finalized 5-minute candles for signals.
- Uses confirmed 3-left/3-right pivots to avoid repainting.
- Scores symmetric long and short liquidity-sweep setups deterministically.
- Stores candles, ticker snapshots, features, reasons, and complete signals in PostgreSQL.
- Sends HTML-safe Telegram alerts only after a signal is committed to the database.
- Optionally maintains a Level-50 local order book for context.
- Exposes `/health`, `/ready`, and interactive API docs at `/docs`.

See [docs/V1_STRATEGY.md](docs/V1_STRATEGY.md) for the complete strategy contract.
See [docs/SIGNAL_DIAGNOSTICS.md](docs/SIGNAL_DIAGNOSTICS.md) for exact-time CVD diagnostics,
immutable signal snapshots, historical audits, and automatic 15/30/60/120-minute outcomes.

## Local setup

Requirements: Python 3.12, Docker, and Docker Compose.

The development PostgreSQL container is published on host port `5433` to avoid conflicts
with locally installed PostgreSQL services that commonly use `5432`.

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn btc_monitor.main:app --reload
```

Open `http://localhost:8000/health`. During a fresh installation, `status` remains
`WARMING_UP` until both markets have accumulated enough one-minute CVD bars. This is
intentional: historical candles may be backfilled, but CVD is never fabricated.
After upgrading, restart the process. Historical minute rows without recorded completeness
do not count toward fresh continuous warmup. Use `/signals` to find signal IDs, then
`/signals/{id}/debug` and `/signals/{id}/outcomes` to inspect diagnostics and results.

Run tests:

```bash
pytest
```

## Telegram

Telegram is off by default. Create a bot with BotFather, obtain the destination chat ID,
and set:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

No secret is committed. If Telegram is disabled, signals are still calculated and stored.

## Railway deployment

1. Push this repository to GitHub and create a Railway project from it.
2. Add a Railway PostgreSQL service.
3. Reference the PostgreSQL `DATABASE_URL` in the app service.
4. Add the environment variables from `.env.example`; add Telegram secrets only if used.
5. Deploy. The Docker start command runs `alembic upgrade head` before Uvicorn.
6. Keep Serverless/App Sleeping disabled because the service maintains WebSockets 24/7.
7. Verify `/health`, application logs, and that both streams become connected.

For new Railway services, explicitly select Dockerfile in Settings and set the start command,
`/health` healthcheck (300 seconds), and On Failure restart policy (10 retries). Do not rely
on `railway.json` being activated: Railway's current console restricts Config as Code to
legacy services. Keep exactly one replica and Serverless disabled. The Dockerfile command
already migrates the database before starting the application.

The unauthenticated signal/debug APIs should remain on private networking unless you
intentionally want their market diagnostics publicly readable. Railway's
deployment check is not continuous monitoring; the runtime also tracks stream staleness,
reconnects, and one-time outage/recovery notifications.

## Reliability model

- Spot and Linear streams reconnect independently.
- Reconnect delay is exponential with jitter: roughly 1, 2, 4, 8, 16, then 30 seconds.
- The client sends application-level ping messages every 20 seconds.
- A stream with no messages for 30 seconds is closed and reconnected.
- An outage lasting 60 seconds generates one warning; recovery generates one message.
- Ticker updates stay in memory and are persisted every five seconds.
- Graceful shutdown flushes in-progress flow bars.
- A signal is persisted before Telegram is attempted.

## Configuration

All tuning is environment-driven. Important settings include pivot widths and age bounds,
CVD warmup bars, OI threshold, funding thresholds, volume multiplier, signal thresholds,
cooldown, stream timers, Telegram, and optional order-book collection. Defaults in
`.env.example` match the V1 specification.

## Operational notes

- Bybit private API credentials are neither accepted nor required.
- PostgreSQL is required in production; SQLite is deliberately unsupported.
- `ORDERBOOK_ENABLED=false` is the default because resting liquidity is cancellable.
- Signal entry, invalidation, and 1R/2R/3R levels are analytical references only.
- This software is informational and is not financial advice.
