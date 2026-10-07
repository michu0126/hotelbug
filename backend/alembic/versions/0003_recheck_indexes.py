"""Indexes for worldwide calendar rechecks and recent stay attempts.

Revision ID: 0003_recheck_indexes
Revises: 0002_stay_scans
"""

from alembic import op

revision = "0003_recheck_indexes"
down_revision = "0002_stay_scans"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_job_stay_attempt", "crawl_jobs", ["hotel_id", "check_in", "check_out", "kind", "created_at"]
    )
    op.create_index("ix_stay_scans_recheck_cursor", "stay_scans", ["observed_at", "id"])
    op.create_index("ix_stay_scans_window", "stay_scans", ["check_in", "observed_at"])


def downgrade():
    op.drop_index("ix_stay_scans_window", table_name="stay_scans")
    op.drop_index("ix_stay_scans_recheck_cursor", table_name="stay_scans")
    op.drop_index("ix_job_stay_attempt", table_name="crawl_jobs")
