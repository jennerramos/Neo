"""Personnel review reason — machine-written flag for why a row needs review

Adds personnel_actions.review_reason.

This is deliberately NOT review_notes.  review_notes belongs to the human audit
trail (reviewed_at / reviewed_by / review_notes) and is written by a person
after they look at a row; review_reason is written by the extractor before any
human sees it, and is overwritten on every re-extract.  Keeping them apart means
a re-run can never clobber a reviewer's note.

Currently the only reason emitted is 'missing_person_name': a personnel action
whose subject could not be identified.  Those rows are preserved (an unnamed
appointment is still evidence that an appointment happened) but they are pinned
below the auto-accept threshold so they cannot reach a trustee unreviewed.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "personnel_actions",
        sa.Column("review_reason", sa.Text(), nullable=True),
    )
    # Partial index: the review queue only ever filters on flagged rows.
    op.create_index(
        "ix_personnel_actions_review_reason",
        "personnel_actions",
        ["review_reason"],
        postgresql_where=sa.text("review_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_personnel_actions_review_reason", table_name="personnel_actions")
    op.drop_column("personnel_actions", "review_reason")
