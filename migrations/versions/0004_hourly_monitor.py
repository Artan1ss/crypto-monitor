"""Independent hourly research signals and immutable feature decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004_hourly_monitor"
down_revision = "0003_market_archive"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "hourly_bars",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", pg.JSONB(), nullable=False),
        sa.Column("evaluated", sa.Boolean(), nullable=False, server_default="false"),
        sa.UniqueConstraint("source", "start"),
    )
    op.create_table(
        "hourly_alerts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("bar_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("entry", sa.Numeric(24, 8), nullable=False),
        sa.Column("invalidation", sa.Numeric(24, 8), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("outcomes", pg.JSONB()),
        sa.Column("tracking_complete", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("delivery_state", sa.String(24), nullable=False),
        sa.Column("delivery_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_id", sa.String(64)),
        sa.UniqueConstraint("source", "bar_start", "direction", "tier"),
    )
    op.create_index("ix_hourly_alerts_generated", "hourly_alerts", ["generated_at"])


def downgrade():
    op.drop_table("hourly_alerts")
    op.drop_table("hourly_bars")
