"""Phase 2 Caption Adapters — schools.default_source_type + discovery_config

Adds two columns to the ``schools`` table so that the per-school adapter
dispatch (pipeline.sources.for_school) and per-platform discovery config
(URLs, folder IDs, ...) live in the DB instead of being hardcoded in
Python.

Columns added:
  default_source_type  TEXT NOT NULL DEFAULT 'youtube_caption'
      Dispatch key matching ``CaptionSourceAdapter.source_type``. Values:
      'youtube_caption', 'panopto', 'ravnur'.

  discovery_config     JSONB NULL
      Free-form per-platform config. Examples:
        Dallas (Ravnur):
          {"portal_url": "https://mediaportal.dallascollege.edu",
           "category": "Board Meetings"}
        ACC (Panopto):
          {"board_page_url": "https://offices.austincc.edu/board-of-trustees/board-meetings/"}
        Alamo (Panopto):
          {"board_page_url": "https://www.alamo.edu/about-us/leadership/board-of-trustees/board-meetings/videos--photos/"}

The legacy ``schools.source_type`` TEXT column is left in place
(deprecated). For existing rows it usually contains 'youtube'; the
backfill below translates that to 'youtube_caption' so the new column
matches the adapter dispatch key.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schools",
        sa.Column(
            "default_source_type",
            sa.Text(),
            nullable=False,
            server_default="youtube_caption",
        ),
    )
    op.add_column(
        "schools",
        sa.Column(
            "discovery_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # Backfill: existing rows seeded with source_type='youtube' map to the
    # adapter dispatch key 'youtube_caption'. Anything else passes through.
    op.execute(
        """
        UPDATE schools
        SET default_source_type = CASE
            WHEN source_type IS NULL                  THEN 'youtube_caption'
            WHEN source_type = 'youtube'              THEN 'youtube_caption'
            ELSE source_type
        END
        """
    )


def downgrade() -> None:
    op.drop_column("schools", "discovery_config")
    op.drop_column("schools", "default_source_type")
