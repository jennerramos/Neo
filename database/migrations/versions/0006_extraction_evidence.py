"""Source-chunk evidence provenance

Adds extraction_evidence: one row per verified quotation linking an extracted
claim to the chunk whose words support it.

Backward compatible by construction:
  - nothing is dropped, renamed, or rewritten
  - votes/financial_items/personnel_actions/initiatives keep evidence_text and
    chunk_ids exactly as they are, so existing API consumers are unaffected
  - rows extracted before this migration simply have no evidence rows; a NULL
    review_reason and an empty evidence set are both legal states

Also backfills review_reason onto the three extraction tables that lack it
(personnel_actions got it in 0005), so every table can record WHY a row was
flagged.  review_reason is machine-written and overwritten on re-extract;
review_notes remains the human audit trail and is never touched by the pipeline.

Parent linkage uses four nullable FKs with a CHECK that exactly one is set,
rather than a (item_type, item_id) pair.  The four extraction tables have
independent identity spaces and separate lifetimes, and each extractor already
deletes-and-rewrites its own rows per meeting; real foreign keys with ON DELETE
CASCADE make evidence follow that lifecycle automatically instead of leaving
orphans behind a soft polymorphic key.

ROLLBACK
    alembic downgrade 0005
  Drops extraction_evidence and the three review_reason columns.  No extraction
  row loses data: evidence_text and chunk_ids were never modified, so the system
  returns to exactly its pre-0006 behaviour.  Evidence rows themselves are lost
  on downgrade — they are derived data, reproducible by re-running the extractor.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PARENTS = ("votes", "financial_items", "personnel_actions", "initiatives")


def upgrade() -> None:
    # ── review_reason on the tables that don't have it yet ─────────────────
    for table in ("votes", "financial_items", "initiatives"):
        op.add_column(table, sa.Column("review_reason", sa.Text(), nullable=True))

    # ── evidence table ─────────────────────────────────────────────────────
    op.create_table(
        "extraction_evidence",
        sa.Column("evidence_id", sa.Integer(), primary_key=True, autoincrement=True),

        # Exactly one of these is set — see the CHECK below.
        sa.Column("vote_id", sa.Integer(),
                  sa.ForeignKey("votes.vote_id", ondelete="CASCADE")),
        sa.Column("financial_item_id", sa.Integer(),
                  sa.ForeignKey("financial_items.item_id", ondelete="CASCADE")),
        sa.Column("personnel_action_id", sa.Integer(),
                  sa.ForeignKey("personnel_actions.action_id", ondelete="CASCADE")),
        sa.Column("initiative_id", sa.Integer(),
                  sa.ForeignKey("initiatives.initiative_id", ondelete="CASCADE")),

        sa.Column("meeting_id", sa.Integer(),
                  sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("chunk_id", sa.Text(),
                  sa.ForeignKey("chunks.chunk_id", ondelete="CASCADE"),
                  nullable=False),

        # The verified quotation, verbatim as it appears in the chunk.
        sa.Column("exact_quote", sa.Text(), nullable=False),
        # Extracted field names this quote supports, e.g. {amount,action_type}.
        sa.Column("supports", sa.ARRAY(sa.Text())),

        sa.Column("start_time_sec", sa.Float()),
        sa.Column("end_time_sec",   sa.Float()),

        # Character offsets of the quote inside the chunk's original text.
        sa.Column("quote_start_char", sa.Integer()),
        sa.Column("quote_end_char",   sa.Integer()),

        # Hash of the chunk text this quote was verified against.  chunk_id is
        # positional (f"{video_id}_{idx:04d}"), so a re-chunk can point the same
        # ID at different words; comparing this on read turns that from a silent
        # wrong answer into a detectable mismatch.
        sa.Column("chunk_sha", sa.Text(), nullable=False),

        # True when the quote lands in text carried in from the previous chunk.
        sa.Column("in_overlap", sa.Boolean(), server_default="false", nullable=False),

        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),

        sa.CheckConstraint(
            "(vote_id IS NOT NULL)::int + (financial_item_id IS NOT NULL)::int + "
            "(personnel_action_id IS NOT NULL)::int + (initiative_id IS NOT NULL)::int = 1",
            name="ck_extraction_evidence_one_parent",
        ),
    )

    op.create_index("ix_extraction_evidence_chunk",   "extraction_evidence", ["chunk_id"])
    op.create_index("ix_extraction_evidence_meeting", "extraction_evidence", ["meeting_id"])
    for parent in _PARENTS:
        col = {"votes": "vote_id", "financial_items": "financial_item_id",
               "personnel_actions": "personnel_action_id",
               "initiatives": "initiative_id"}[parent]
        op.create_index(f"ix_extraction_evidence_{col}", "extraction_evidence", [col])

    # One quote per parent per chunk.  Dedup is enforced in pipeline/evidence.py
    # on the normalized quote; this is the backstop against a double insert.
    op.create_index(
        "ux_extraction_evidence_dedup",
        "extraction_evidence",
        ["vote_id", "financial_item_id", "personnel_action_id",
         "initiative_id", "chunk_id", "exact_quote"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("extraction_evidence")
    for table in ("votes", "financial_items", "initiatives"):
        op.drop_column(table, "review_reason")
