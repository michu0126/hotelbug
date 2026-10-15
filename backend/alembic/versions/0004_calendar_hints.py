"""Keep public calendar hints separate from dated room quote history."""

import sqlalchemy as sa

from alembic import op

revision = "0004_calendar_hints"
down_revision = "0003_recheck_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_hint_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("hotel_id", sa.String(36), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("series_key", sa.String(64), nullable=False),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("adults", sa.Integer(), nullable=False),
        sa.Column("rooms", sa.Integer(), nullable=False),
        sa.Column("displayed_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("basis", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_response_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("job_id", "check_in"),
    )
    op.create_index("ix_hint_series_time", "calendar_hint_history", ["series_key", "captured_at"])
    op.create_index("ix_hint_hotel_date", "calendar_hint_history", ["hotel_id", "check_in"])


def downgrade():
    op.drop_index("ix_hint_hotel_date", table_name="calendar_hint_history")
    op.drop_index("ix_hint_series_time", table_name="calendar_hint_history")
    op.drop_table("calendar_hint_history")
