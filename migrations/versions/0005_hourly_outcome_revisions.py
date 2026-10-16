"""Append-only hourly outcome and score revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005_hourly_outcome_revisions"
down_revision = "0004_hourly_monitor"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "hourly_outcome_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("alert_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("alert_id", "fingerprint"),
    )
    op.create_index("ix_hourly_revision_alert", "hourly_outcome_revisions", ["alert_id", "id"])


def downgrade():
    op.drop_table("hourly_outcome_revisions")
