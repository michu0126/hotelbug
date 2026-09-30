"""Track successful stay checks, including no availability.

Revision ID: 0002_stay_scans
Revises: 0001_foundation
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_stay_scans"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stay_scans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("hotel_id", sa.String(length=36), sa.ForeignKey("hotels.id"), nullable=False),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("check_out", sa.Date(), nullable=False),
        sa.Column("adults", sa.Integer(), nullable=False),
        sa.Column("rooms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.UniqueConstraint("hotel_id", "check_in", "check_out", "adults", "rooms"),
    )
    op.create_index("ix_stay_scans_hotel_date", "stay_scans", ["hotel_id", "check_in"])


def downgrade():
    op.drop_index("ix_stay_scans_hotel_date", table_name="stay_scans")
    op.drop_table("stay_scans")
