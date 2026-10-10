"""Persist data completeness, immutable signal snapshots and result tracking."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_signal_diagnostics"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means historical completeness was not recorded; never invent that evidence.
    op.add_column("market_flow_1m", sa.Column("is_complete", sa.Boolean(), nullable=True))
    op.add_column("signals", sa.Column("telegram_sent_at", sa.DateTime(timezone=True)))
    op.add_column("signals", sa.Column("debug_snapshot", postgresql.JSONB()))
    op.add_column("signals", sa.Column("outcomes", postgresql.JSONB()))
    op.add_column(
        "signals",
        sa.Column("tracking_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_signals_tracking_complete", "signals", ["tracking_complete"])


def downgrade() -> None:
    op.drop_index("ix_signals_tracking_complete", table_name="signals")
    for name in ("tracking_complete", "outcomes", "debug_snapshot", "telegram_sent_at"):
        op.drop_column("signals", name)
    op.drop_column("market_flow_1m", "is_complete")
