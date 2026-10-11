"""Independent, append-only multi-venue research archive."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_market_archive"
down_revision = "0002_signal_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("venue", sa.String(24), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("instrument", sa.String(48), nullable=False),
        sa.Column("dataset", sa.String(24), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(
        "ix_observation_series_time",
        "market_observations",
        ["venue", "market", "dataset", "event_time"],
    )
    op.create_index("ix_observation_available", "market_observations", ["available_at", "id"])


def downgrade() -> None:
    op.drop_table("market_observations")
