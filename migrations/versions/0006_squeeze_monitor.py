"""Persistent V2 squeeze evidence, incremental episodes and at-most-once outbox."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006_squeeze_monitor"
down_revision = "0005_hourly_outcome_revisions"
branch_labels = depends_on = None


def upgrade():
    op.create_table("squeeze_runtime",
        sa.Column("version", sa.String(80), primary_key=True),
        sa.Column("state", pg.JSONB(), nullable=False))
    op.create_table("squeeze_alerts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_key", sa.String(256), nullable=False),
        sa.Column("bar_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("delivery_state", sa.String(32), nullable=False),
        sa.Column("delivery_id", sa.String(64)),
        sa.Column("delivery_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_key"))
    op.create_index("ix_squeeze_generated", "squeeze_alerts", ["generated_at"])


def downgrade():
    op.drop_table("squeeze_alerts")
    op.drop_table("squeeze_runtime")
