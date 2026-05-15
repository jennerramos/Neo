"""Phase 6 + 6.5 — Full extraction schema rebuild

Drops the minimal votes/financial_items/personnel_actions tables from the
initial schema and replaces them with fully-provenance-tracked versions.
Also adds Phase 6.5 tables: initiatives, pattern_signals.

Every extraction row now carries:
  - denormalized school_id, school_slug, video_id (no join needed)
  - chunk_ids TEXT[]   (jsonl chunk IDs that form the evidence window)
  - evidence_text      (verbatim excerpt ≤500 chars)
  - source_type        ('vtt' | 'whisperx')
  - start_time_sec / end_time_sec
  - extractor_version
  - confidence         (0.0–1.0)
  - needs_review       (auto-flagged when confidence < threshold)
  - reviewed_at / reviewed_by / review_notes (human audit trail)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Drop old minimal extraction tables (CASCADE removes any FKs)
    # ------------------------------------------------------------------
    op.drop_table("votes")
    op.drop_table("financial_items")
    op.drop_table("personnel_actions")

    # ------------------------------------------------------------------
    # 2. votes  (Phase 6)
    # ------------------------------------------------------------------
    op.create_table(
        "votes",
        sa.Column("vote_id",          sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("meeting_id",        sa.Integer(),  sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("school_id",         sa.Integer(),  sa.ForeignKey("schools.school_id",  ondelete="CASCADE"), nullable=False),
        sa.Column("school_slug",        sa.Text(),     nullable=False),
        sa.Column("video_id",          sa.Text(),     nullable=False),
        # Core event
        sa.Column("agenda_item",        sa.Text()),
        sa.Column("motion_text",        sa.Text()),
        sa.Column("topic_category",     sa.Text()),   # fixed vocab
        # People
        sa.Column("moved_by",           sa.Text()),
        sa.Column("seconded_by",        sa.Text()),
        # Result
        sa.Column("vote_result_text",   sa.Text()),
        sa.Column("yes_count",          sa.Integer()),
        sa.Column("no_count",           sa.Integer()),
        sa.Column("abstain_count",      sa.Integer()),
        sa.Column("absent_count",       sa.Integer()),
        sa.Column("passed",             sa.Boolean()),
        sa.Column("unanimous",          sa.Boolean()),
        # Provenance
        sa.Column("chunk_ids",          postgresql.ARRAY(sa.Text())),
        sa.Column("evidence_text",      sa.Text()),
        sa.Column("source_type",        sa.Text()),
        sa.Column("start_time_sec",     sa.Float()),
        sa.Column("end_time_sec",       sa.Float()),
        # Quality / audit
        sa.Column("extractor_version",  sa.Text(), nullable=False, server_default="v2.0"),
        sa.Column("confidence",         sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("needs_review",       sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at",        sa.DateTime(timezone=True)),
        sa.Column("reviewed_by",        sa.Text()),
        sa.Column("review_notes",       sa.Text()),
    )
    op.create_index("ix_votes_meeting_id",  "votes", ["meeting_id"])
    op.create_index("ix_votes_school_id",   "votes", ["school_id"])
    op.create_index("ix_votes_school_slug", "votes", ["school_slug"])
    op.create_index("ix_votes_needs_review","votes", ["needs_review"])

    # ------------------------------------------------------------------
    # 3. financial_items  (Phase 6)
    # ------------------------------------------------------------------
    op.create_table(
        "financial_items",
        sa.Column("item_id",           sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("meeting_id",         sa.Integer(),  sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("school_id",          sa.Integer(),  sa.ForeignKey("schools.school_id",  ondelete="CASCADE"), nullable=False),
        sa.Column("school_slug",         sa.Text(),    nullable=False),
        sa.Column("video_id",           sa.Text(),    nullable=False),
        # Core event
        sa.Column("amount",             sa.Numeric(15, 2)),
        sa.Column("currency",           sa.Text(), server_default="USD"),
        sa.Column("category",           sa.Text()),   # fixed vocab
        sa.Column("description",        sa.Text()),
        sa.Column("vendor",             sa.Text()),
        sa.Column("fund_source",        sa.Text()),
        sa.Column("fiscal_year",        sa.Text()),
        sa.Column("action_type",        sa.Text()),   # approved|proposed|discussed|tabled|amended
        # Provenance
        sa.Column("chunk_ids",          postgresql.ARRAY(sa.Text())),
        sa.Column("evidence_text",      sa.Text()),
        sa.Column("source_type",        sa.Text()),
        sa.Column("start_time_sec",     sa.Float()),
        sa.Column("end_time_sec",       sa.Float()),
        # Quality / audit
        sa.Column("extractor_version",  sa.Text(), nullable=False, server_default="v2.0"),
        sa.Column("confidence",         sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("needs_review",       sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at",        sa.DateTime(timezone=True)),
        sa.Column("reviewed_by",        sa.Text()),
        sa.Column("review_notes",       sa.Text()),
    )
    op.create_index("ix_financial_items_meeting_id",  "financial_items", ["meeting_id"])
    op.create_index("ix_financial_items_school_id",   "financial_items", ["school_id"])
    op.create_index("ix_financial_items_school_slug", "financial_items", ["school_slug"])
    op.create_index("ix_financial_items_category",    "financial_items", ["category"])

    # ------------------------------------------------------------------
    # 4. personnel_actions  (Phase 6)
    # ------------------------------------------------------------------
    op.create_table(
        "personnel_actions",
        sa.Column("action_id",         sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("meeting_id",         sa.Integer(),  sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("school_id",          sa.Integer(),  sa.ForeignKey("schools.school_id",  ondelete="CASCADE"), nullable=False),
        sa.Column("school_slug",         sa.Text(),    nullable=False),
        sa.Column("video_id",           sa.Text(),    nullable=False),
        # Core event
        sa.Column("person_name",        sa.Text()),   # nullable — transcripts often omit names
        sa.Column("action_type",        sa.Text()),   # hire|appoint|promote|terminate|resign|retire|interim
        sa.Column("position",           sa.Text()),
        sa.Column("department",         sa.Text()),
        sa.Column("effective_date",     sa.Text()),   # TEXT not DATE — transcripts are imprecise
        sa.Column("is_interim",         sa.Boolean(), server_default="false"),
        # Provenance
        sa.Column("chunk_ids",          postgresql.ARRAY(sa.Text())),
        sa.Column("evidence_text",      sa.Text()),
        sa.Column("source_type",        sa.Text()),
        sa.Column("start_time_sec",     sa.Float()),
        sa.Column("end_time_sec",       sa.Float()),
        # Quality / audit
        sa.Column("extractor_version",  sa.Text(), nullable=False, server_default="v2.0"),
        sa.Column("confidence",         sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("needs_review",       sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at",        sa.DateTime(timezone=True)),
        sa.Column("reviewed_by",        sa.Text()),
        sa.Column("review_notes",       sa.Text()),
    )
    op.create_index("ix_personnel_actions_meeting_id",  "personnel_actions", ["meeting_id"])
    op.create_index("ix_personnel_actions_school_id",   "personnel_actions", ["school_id"])
    op.create_index("ix_personnel_actions_action_type", "personnel_actions", ["action_type"])

    # ------------------------------------------------------------------
    # 5. initiatives  (Phase 6.5)
    # ------------------------------------------------------------------
    op.create_table(
        "initiatives",
        sa.Column("initiative_id",     sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("meeting_id",         sa.Integer(),  sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("school_id",          sa.Integer(),  sa.ForeignKey("schools.school_id",  ondelete="CASCADE"), nullable=False),
        sa.Column("school_slug",         sa.Text(),    nullable=False),
        sa.Column("video_id",           sa.Text(),    nullable=False),
        # Core
        sa.Column("initiative_name",    sa.Text(),    nullable=False),
        sa.Column("category",           sa.Text(),    nullable=False),
        sa.Column("description",        sa.Text()),
        # Evidence hierarchy — NEVER conflate these four fields
        sa.Column("observed_action",    sa.Text()),   # "Board approved aviation program"
        sa.Column("stated_rationale",   sa.Text()),   # "College said this addresses workforce gap"
        sa.Column("claimed_outcome",    sa.Text()),   # "College claimed enrollment would rise 20%"
        sa.Column("measured_outcome",   sa.Text()),   # ONLY if hard numbers appear in transcript
        sa.Column("outcome_timeframe",  sa.Text()),   # "by Fall 2025", "within 2 years"
        # Action status
        sa.Column("action_type",        sa.Text()),   # launched|proposed|approved|discussed|continued|cancelled
        # Provenance
        sa.Column("chunk_ids",          postgresql.ARRAY(sa.Text())),
        sa.Column("evidence_text",      sa.Text()),
        sa.Column("source_type",        sa.Text()),
        sa.Column("start_time_sec",     sa.Float()),
        sa.Column("end_time_sec",       sa.Float()),
        # Quality / audit
        sa.Column("extractor_version",  sa.Text(), nullable=False, server_default="v2.5"),
        sa.Column("confidence",         sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("needs_review",       sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at",         sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_initiatives_meeting_id",  "initiatives", ["meeting_id"])
    op.create_index("ix_initiatives_school_id",   "initiatives", ["school_id"])
    op.create_index("ix_initiatives_school_slug", "initiatives", ["school_slug"])
    op.create_index("ix_initiatives_category",    "initiatives", ["category"])

    # ------------------------------------------------------------------
    # 6. pattern_signals  (Phase 6.5 — aggregated, not per-meeting)
    # ------------------------------------------------------------------
    op.create_table(
        "pattern_signals",
        sa.Column("signal_id",          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signal_type",         sa.Text(),   nullable=False),  # recurring_initiative|budget_trend|...
        sa.Column("category",            sa.Text(),   nullable=False),
        sa.Column("description",         sa.Text(),   nullable=False),
        sa.Column("school_count",         sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meeting_count",        sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_observed_date",  sa.Date()),
        sa.Column("last_observed_date",   sa.Date()),
        sa.Column("supporting_initiative_ids", postgresql.ARRAY(sa.Integer())),
        sa.Column("extractor_version",   sa.Text(), nullable=False, server_default="v2.5"),
        sa.Column("confidence",          sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("needs_review",        sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at",          sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_pattern_signals_category",   "pattern_signals", ["category"])
    op.create_index("ix_pattern_signals_signal_type","pattern_signals", ["signal_type"])


def downgrade() -> None:
    op.drop_table("pattern_signals")
    op.drop_table("initiatives")
    op.drop_table("personnel_actions")
    op.drop_table("financial_items")
    op.drop_table("votes")

    # Restore minimal original tables
    op.create_table(
        "votes",
        sa.Column("vote_id",      sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id",   sa.Integer(), sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("agenda_item",  sa.Text()),
        sa.Column("motion_by",    sa.Text()),
        sa.Column("seconded_by",  sa.Text()),
        sa.Column("ayes",         sa.Integer()),
        sa.Column("nays",         sa.Integer()),
        sa.Column("abstentions",  sa.Integer()),
        sa.Column("result",       sa.Text()),
        sa.Column("timestamp_sec",sa.Float()),
    )
    op.create_table(
        "financial_items",
        sa.Column("item_id",      sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id",   sa.Integer(), sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount",       sa.Numeric(15, 2)),
        sa.Column("context",      sa.Text()),
        sa.Column("category",     sa.Text()),
        sa.Column("timestamp_sec",sa.Float()),
    )
    op.create_table(
        "personnel_actions",
        sa.Column("action_id",    sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id",   sa.Integer(), sa.ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type",  sa.Text()),
        sa.Column("position",     sa.Text()),
        sa.Column("department",   sa.Text()),
        sa.Column("timestamp_sec",sa.Float()),
    )
