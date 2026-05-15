"""
Neo v2 — SQLAlchemy ORM Models
Phase 6 + 6.5 schema — full provenance extraction tables.

Tables:
    schools, channels, meetings, pipeline_runs, chunks,
    votes, financial_items, personnel_actions,          ← Phase 6
    initiatives, pattern_signals,                       ← Phase 6.5
    query_logs
"""
import uuid
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey,
    Integer, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 1. schools
# ---------------------------------------------------------------------------

class School(Base):
    __tablename__ = "schools"

    school_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    website: Mapped[Optional[str]] = mapped_column(Text)
    state: Mapped[Optional[str]] = mapped_column(Text, server_default="TX")
    source_type: Mapped[Optional[str]] = mapped_column(Text)
    # Adapter dispatch key — matches CaptionSourceAdapter.source_type values
    # registered in pipeline.sources.ADAPTERS. The legacy ``source_type`` above
    # is kept for compatibility but no longer drives dispatch.
    default_source_type: Mapped[str] = mapped_column(
        Text, server_default="youtube_caption", nullable=False
    )
    # Free-form per-platform discovery config (URL, folder ID, ...). Schema
    # is platform-specific; see pipeline.sources.<platform>.
    discovery_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    channels: Mapped[List["Channel"]] = relationship("Channel", back_populates="school")
    meetings: Mapped[List["Meeting"]] = relationship("Meeting", back_populates="school")

    def __repr__(self) -> str:
        return f"<School school_id={self.school_id} slug={self.slug!r}>"


# ---------------------------------------------------------------------------
# 2. channels
# ---------------------------------------------------------------------------

class Channel(Base):
    __tablename__ = "channels"

    channel_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    youtube_channel_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    channel_name: Mapped[Optional[str]] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    school: Mapped["School"] = relationship("School", back_populates="channels")

    def __repr__(self) -> str:
        return f"<Channel channel_id={self.channel_id} yt={self.youtube_channel_id!r}>"


# ---------------------------------------------------------------------------
# 3. meetings
# ---------------------------------------------------------------------------

class Meeting(Base):
    __tablename__ = "meetings"

    meeting_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    video_url: Mapped[Optional[str]] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(Text)
    published_date: Mapped[Optional[date]] = mapped_column(Date)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    meeting_type: Mapped[Optional[str]] = mapped_column(Text)

    # Pipeline state machine — quick reference. Authoritative spec (per-phase
    # eligibility, terminal failures, recovery routing) lives in pipeline.states.
    #
    #   discovered → captioned / needs_asr → transcribed
    #              → processed → extracted → approved → indexed
    #
    # Terminal-failure states (no automatic recovery — see scripts/recover_failed.py):
    #   asr_failed          — WhisperX failed in Phase 4
    #   processing_failed   — packager failed in Phase 5
    #   rejected            — quality_gate rejected (re-runnable via `quality_gate --recheck`)
    #   caption_unavailable — Phase 3 confirmed the platform has no captions
    #                         AND no audio download path, so ASR isn't an option.
    #                         Truly terminal — no recovery exists.
    #
    # The diamond between extracted/approved is intentional: quality_gate and
    # the structured extractor are order-independent. The indexer accepts either.
    status: Mapped[str] = mapped_column(Text, server_default="discovered", nullable=False)

    source_type: Mapped[Optional[str]] = mapped_column(Text)

    raw_vtt_path: Mapped[Optional[str]] = mapped_column(Text)
    clean_txt_path: Mapped[Optional[str]] = mapped_column(Text)
    meeting_json_path: Mapped[Optional[str]] = mapped_column(Text)
    chunks_jsonl_path: Mapped[Optional[str]] = mapped_column(Text)

    word_count: Mapped[Optional[int]] = mapped_column(Integer)
    speaker_count: Mapped[Optional[int]] = mapped_column(Integer)
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    processing_version: Mapped[Optional[str]] = mapped_column(Text)
    file_hash: Mapped[Optional[str]] = mapped_column(Text)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    school: Mapped["School"] = relationship("School", back_populates="meetings")
    pipeline_runs: Mapped[List["PipelineRun"]] = relationship(
        "PipelineRun", back_populates="meeting", cascade="all, delete-orphan"
    )
    chunks: Mapped[List["Chunk"]] = relationship(
        "Chunk", back_populates="meeting", cascade="all, delete-orphan"
    )
    votes: Mapped[List["Vote"]] = relationship(
        "Vote", back_populates="meeting", cascade="all, delete-orphan"
    )
    financial_items: Mapped[List["FinancialItem"]] = relationship(
        "FinancialItem", back_populates="meeting", cascade="all, delete-orphan"
    )
    personnel_actions: Mapped[List["PersonnelAction"]] = relationship(
        "PersonnelAction", back_populates="meeting", cascade="all, delete-orphan"
    )
    initiatives: Mapped[List["Initiative"]] = relationship(
        "Initiative", back_populates="meeting", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Meeting meeting_id={self.meeting_id} video_id={self.video_id!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# 4. pipeline_runs
# ---------------------------------------------------------------------------

class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=True
    )
    channel_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("channels.channel_id", ondelete="CASCADE"), nullable=True
    )
    phase: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    gpu_time_seconds: Mapped[Optional[float]] = mapped_column(Float)
    memory_mb: Mapped[Optional[float]] = mapped_column(Float)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="pipeline_runs")

    def __repr__(self) -> str:
        return f"<PipelineRun run_id={self.run_id} phase={self.phase!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# 5. chunks
# ---------------------------------------------------------------------------

class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(
        Text, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    meeting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False
    )
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[Optional[int]] = mapped_column(Integer)
    text: Mapped[Optional[str]] = mapped_column(Text)
    speaker: Mapped[Optional[str]] = mapped_column(Text)
    start_time: Mapped[Optional[float]] = mapped_column(Float)
    end_time: Mapped[Optional[float]] = mapped_column(Float)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    topic_label: Mapped[Optional[str]] = mapped_column(Text)
    qdrant_indexed: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    processing_version: Mapped[Optional[str]] = mapped_column(Text)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="chunks")

    def __repr__(self) -> str:
        return f"<Chunk chunk_id={self.chunk_id[:8]}... idx={self.chunk_index}>"


# ---------------------------------------------------------------------------
# 6. votes  (Phase 6)
# ---------------------------------------------------------------------------

class Vote(Base):
    __tablename__ = "votes"

    vote_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False
    )
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    school_slug: Mapped[str] = mapped_column(Text, nullable=False)
    video_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Core event
    agenda_item: Mapped[Optional[str]] = mapped_column(Text)
    motion_text: Mapped[Optional[str]] = mapped_column(Text)
    topic_category: Mapped[Optional[str]] = mapped_column(Text)

    # People
    moved_by: Mapped[Optional[str]] = mapped_column(Text)
    seconded_by: Mapped[Optional[str]] = mapped_column(Text)

    # Result
    vote_result_text: Mapped[Optional[str]] = mapped_column(Text)
    yes_count: Mapped[Optional[int]] = mapped_column(Integer)
    no_count: Mapped[Optional[int]] = mapped_column(Integer)
    abstain_count: Mapped[Optional[int]] = mapped_column(Integer)
    absent_count: Mapped[Optional[int]] = mapped_column(Integer)
    passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    unanimous: Mapped[Optional[bool]] = mapped_column(Boolean)

    # Provenance
    chunk_ids: Mapped[Optional[list]] = mapped_column(ARRAY(Text))
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(Text)
    start_time_sec: Mapped[Optional[float]] = mapped_column(Float)
    end_time_sec: Mapped[Optional[float]] = mapped_column(Float)

    # Quality / audit
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v2.0")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text)
    review_notes: Mapped[Optional[str]] = mapped_column(Text)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="votes")

    def __repr__(self) -> str:
        return f"<Vote vote_id={self.vote_id} passed={self.passed} conf={self.confidence:.2f}>"


# ---------------------------------------------------------------------------
# 7. financial_items  (Phase 6)
# ---------------------------------------------------------------------------

class FinancialItem(Base):
    __tablename__ = "financial_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False
    )
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    school_slug: Mapped[str] = mapped_column(Text, nullable=False)
    video_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Core event
    amount: Mapped[Optional[float]] = mapped_column(Numeric(15, 2))
    currency: Mapped[Optional[str]] = mapped_column(Text, server_default="USD")
    category: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    vendor: Mapped[Optional[str]] = mapped_column(Text)
    fund_source: Mapped[Optional[str]] = mapped_column(Text)
    fiscal_year: Mapped[Optional[str]] = mapped_column(Text)
    action_type: Mapped[Optional[str]] = mapped_column(Text)

    # Provenance
    chunk_ids: Mapped[Optional[list]] = mapped_column(ARRAY(Text))
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(Text)
    start_time_sec: Mapped[Optional[float]] = mapped_column(Float)
    end_time_sec: Mapped[Optional[float]] = mapped_column(Float)

    # Quality / audit
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v2.0")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text)
    review_notes: Mapped[Optional[str]] = mapped_column(Text)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="financial_items")

    def __repr__(self) -> str:
        return f"<FinancialItem item_id={self.item_id} amount={self.amount} action={self.action_type!r}>"


# ---------------------------------------------------------------------------
# 8. personnel_actions  (Phase 6)
# ---------------------------------------------------------------------------

class PersonnelAction(Base):
    __tablename__ = "personnel_actions"

    action_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False
    )
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    school_slug: Mapped[str] = mapped_column(Text, nullable=False)
    video_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Core event
    person_name: Mapped[Optional[str]] = mapped_column(Text)
    action_type: Mapped[Optional[str]] = mapped_column(Text)
    position: Mapped[Optional[str]] = mapped_column(Text)
    department: Mapped[Optional[str]] = mapped_column(Text)
    effective_date: Mapped[Optional[str]] = mapped_column(Text)
    is_interim: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)

    # Provenance
    chunk_ids: Mapped[Optional[list]] = mapped_column(ARRAY(Text))
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(Text)
    start_time_sec: Mapped[Optional[float]] = mapped_column(Float)
    end_time_sec: Mapped[Optional[float]] = mapped_column(Float)

    # Quality / audit
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v2.0")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text)
    review_notes: Mapped[Optional[str]] = mapped_column(Text)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="personnel_actions")

    def __repr__(self) -> str:
        return f"<PersonnelAction action_id={self.action_id} type={self.action_type!r} pos={self.position!r}>"


# ---------------------------------------------------------------------------
# 9. initiatives  (Phase 6.5)
# ---------------------------------------------------------------------------

class Initiative(Base):
    __tablename__ = "initiatives"

    initiative_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meetings.meeting_id", ondelete="CASCADE"), nullable=False
    )
    school_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schools.school_id", ondelete="CASCADE"), nullable=False
    )
    school_slug: Mapped[str] = mapped_column(Text, nullable=False)
    video_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Core
    initiative_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Evidence hierarchy — four distinct claim levels
    observed_action: Mapped[Optional[str]] = mapped_column(Text)   # what the board actually did
    stated_rationale: Mapped[Optional[str]] = mapped_column(Text)  # what they said motivated it
    claimed_outcome: Mapped[Optional[str]] = mapped_column(Text)   # what they predicted would happen
    measured_outcome: Mapped[Optional[str]] = mapped_column(Text)  # hard numbers in the transcript
    outcome_timeframe: Mapped[Optional[str]] = mapped_column(Text)

    # Action status
    action_type: Mapped[Optional[str]] = mapped_column(Text)

    # Provenance
    chunk_ids: Mapped[Optional[list]] = mapped_column(ARRAY(Text))
    evidence_text: Mapped[Optional[str]] = mapped_column(Text)
    source_type: Mapped[Optional[str]] = mapped_column(Text)
    start_time_sec: Mapped[Optional[float]] = mapped_column(Float)
    end_time_sec: Mapped[Optional[float]] = mapped_column(Float)

    # Quality
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v2.5")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="initiatives")

    def __repr__(self) -> str:
        return f"<Initiative initiative_id={self.initiative_id} name={self.initiative_name!r}>"


# ---------------------------------------------------------------------------
# 10. pattern_signals  (Phase 6.5 — aggregated, not per-meeting)
# ---------------------------------------------------------------------------

class PatternSignal(Base):
    __tablename__ = "pattern_signals"

    signal_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    school_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    meeting_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_observed_date: Mapped[Optional[date]] = mapped_column(Date)
    last_observed_date: Mapped[Optional[date]] = mapped_column(Date)
    supporting_initiative_ids: Mapped[Optional[list]] = mapped_column(ARRAY(Integer))
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v2.5")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PatternSignal signal_id={self.signal_id} type={self.signal_type!r} cat={self.category!r}>"


# ---------------------------------------------------------------------------
# 11. query_logs
# ---------------------------------------------------------------------------

class QueryLog(Base):
    __tablename__ = "query_logs"

    query_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[str]] = mapped_column(Text)
    school_filter: Mapped[Optional[str]] = mapped_column(Text)
    question: Mapped[Optional[str]] = mapped_column(Text)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    chunks_used: Mapped[Optional[int]] = mapped_column(Integer)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<QueryLog query_id={self.query_id} latency_ms={self.latency_ms}>"
