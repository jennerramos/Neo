"""Initial schema — all 9 Neo v2 tables

Revision ID: 0001
Revises:
Create Date: 2026-04-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── 1. schools ───────────────────────────────────────────────────────────
    op.create_table(
        "schools",
        sa.Column("school_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="TX", nullable=True),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("school_id"),
        sa.UniqueConstraint("slug"),
    )

    # ── 2. channels ──────────────────────────────────────────────────────────
    op.create_table(
        "channels",
        sa.Column("channel_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("school_id", sa.Integer(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("channel_name", sa.Text(), nullable=True),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["school_id"], ["schools.school_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("channel_id"),
        sa.UniqueConstraint("youtube_channel_id"),
    )
    op.create_index("ix_channels_school_id", "channels", ["school_id"])

    # ── 3. meetings ──────────────────────────────────────────────────────────
    op.create_table(
        "meetings",
        sa.Column("meeting_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("school_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        sa.Column("video_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("meeting_type", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="discovered", nullable=False),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("raw_vtt_path", sa.Text(), nullable=True),
        sa.Column("clean_txt_path", sa.Text(), nullable=True),
        sa.Column("meeting_json_path", sa.Text(), nullable=True),
        sa.Column("chunks_jsonl_path", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("speaker_count", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("processing_version", sa.Text(), nullable=True),
        sa.Column("file_hash", sa.Text(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.school_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("meeting_id"),
        sa.UniqueConstraint("video_id"),
    )
    op.create_index("ix_meetings_school_id", "meetings", ["school_id"])
    op.create_index("ix_meetings_status", "meetings", ["status"])

    # ── 4. pipeline_runs ─────────────────────────────────────────────────────
    op.create_table(
        "pipeline_runs",
        sa.Column("run_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gpu_time_seconds", sa.Float(), nullable=True),
        sa.Column("memory_mb", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.meeting_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_pipeline_runs_meeting_id", "pipeline_runs", ["meeting_id"])

    # ── 5. chunks ────────────────────────────────────────────────────────────
    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.Text(), nullable=False),   # UUID string
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("school_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("speaker", sa.Text(), nullable=True),
        sa.Column("start_time", sa.Float(), nullable=True),
        sa.Column("end_time", sa.Float(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("topic_label", sa.Text(), nullable=True),
        sa.Column("qdrant_indexed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("processing_version", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.meeting_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.school_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index("ix_chunks_meeting_id", "chunks", ["meeting_id"])
    op.create_index("ix_chunks_school_id", "chunks", ["school_id"])
    op.create_index("ix_chunks_qdrant_indexed", "chunks", ["qdrant_indexed"])

    # ── 6. votes ─────────────────────────────────────────────────────────────
    op.create_table(
        "votes",
        sa.Column("vote_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("agenda_item", sa.Text(), nullable=True),
        sa.Column("motion_by", sa.Text(), nullable=True),
        sa.Column("seconded_by", sa.Text(), nullable=True),
        sa.Column("ayes", sa.Integer(), nullable=True),
        sa.Column("nays", sa.Integer(), nullable=True),
        sa.Column("abstentions", sa.Integer(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("timestamp_sec", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.meeting_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vote_id"),
    )
    op.create_index("ix_votes_meeting_id", "votes", ["meeting_id"])

    # ── 7. financial_items ───────────────────────────────────────────────────
    op.create_table(
        "financial_items",
        sa.Column("item_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("timestamp_sec", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.meeting_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id"),
    )
    op.create_index("ix_financial_items_meeting_id", "financial_items", ["meeting_id"])

    # ── 8. personnel_actions ─────────────────────────────────────────────────
    op.create_table(
        "personnel_actions",
        sa.Column("action_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=True),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("timestamp_sec", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.meeting_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("action_id"),
    )
    op.create_index("ix_personnel_actions_meeting_id", "personnel_actions", ["meeting_id"])

    # ── 9. query_logs ────────────────────────────────────────────────────────
    op.create_table(
        "query_logs",
        sa.Column("query_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("school_filter", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("chunks_used", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("query_id"),
    )


def downgrade() -> None:
    op.drop_table("query_logs")
    op.drop_table("personnel_actions")
    op.drop_table("financial_items")
    op.drop_table("votes")
    op.drop_table("chunks")
    op.drop_table("pipeline_runs")
    op.drop_table("meetings")
    op.drop_table("channels")
    op.drop_table("schools")
