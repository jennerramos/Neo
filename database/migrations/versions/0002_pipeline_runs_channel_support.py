"""Make pipeline_runs.meeting_id nullable, add channel_id

Phase 1 channel validation runs before any meetings exist, so
pipeline_runs needs to support channel-level logging too.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Make meeting_id nullable — some runs are channel-level, not meeting-level
    op.alter_column("pipeline_runs", "meeting_id", nullable=True)

    # Add optional channel_id for channel-level pipeline runs
    op.add_column(
        "pipeline_runs",
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.channel_id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_pipeline_runs_channel_id", "pipeline_runs", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_channel_id", "pipeline_runs")
    op.drop_column("pipeline_runs", "channel_id")
    op.alter_column("pipeline_runs", "meeting_id", nullable=False)
