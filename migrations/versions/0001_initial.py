"""Initial BTC Signal Monitor V1 schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_flow_1m",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("buy_notional", sa.Numeric(30, 8), nullable=False),
        sa.Column("sell_notional", sa.Numeric(30, 8), nullable=False),
        sa.Column("delta_notional", sa.Numeric(30, 8), nullable=False),
        sa.Column("cumulative_cvd", sa.Numeric(34, 8), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market", "symbol", "bucket_start"),
    )
    op.create_table(
        "candles_5m",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("turnover", sa.Numeric(30, 8), nullable=False),
        sa.Column("atr14", sa.Numeric(24, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "timestamp"),
    )
    op.create_table(
        "candles_1h",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("ema50", sa.Numeric(24, 8)),
        sa.Column("ema200", sa.Numeric(24, 8)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "timestamp"),
    )
    op.create_table(
        "ticker_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("last_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("mark_price", sa.Numeric(24, 8)),
        sa.Column("index_price", sa.Numeric(24, 8)),
        sa.Column("open_interest", sa.Numeric(30, 8)),
        sa.Column("open_interest_value", sa.Numeric(30, 8)),
        sa.Column("funding_rate", sa.Numeric(18, 10)),
        sa.Column("next_funding_time", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ticker_symbol_timestamp", "ticker_snapshots", ["symbol", "timestamp"])
    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("score", sa.Numeric(8, 3), nullable=False),
        sa.Column("strength", sa.String(16), nullable=False),
        sa.Column("entry_reference", sa.Numeric(24, 8), nullable=False),
        sa.Column("stop_reference", sa.Numeric(24, 8), nullable=False),
        sa.Column("tp1", sa.Numeric(24, 8), nullable=False),
        sa.Column("tp2", sa.Numeric(24, 8), nullable=False),
        sa.Column("tp3", sa.Numeric(24, 8), nullable=False),
        sa.Column("reference_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("reference_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sweep_extreme", sa.Numeric(24, 8), nullable=False),
        sa.Column("strategy_version", sa.String(16), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("telegram_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("telegram_message_id", sa.String(64)),
        sa.UniqueConstraint("direction", "reference_timestamp", "reference_price"),
    )
    op.create_index("ix_signals_signal_time", "signals", ["signal_time"])
    op.create_index("ix_signals_direction", "signals", ["direction"])
    op.create_index("ix_signals_reference_timestamp", "signals", ["reference_timestamp"])


def downgrade() -> None:
    op.drop_table("signals")
    op.drop_table("ticker_snapshots")
    op.drop_table("candles_1h")
    op.drop_table("candles_5m")
    op.drop_table("market_flow_1m")
