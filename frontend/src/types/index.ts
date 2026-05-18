// ── Shared types ─────────────────────────────────────────────────────────────

export interface School {
  school_id: number;
  slug: string;
  name: string;
  website: string | null;
  state: string | null;
}

export interface Pagination {
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

// ── Meetings ──────────────────────────────────────────────────────────────────

export interface Meeting {
  meeting_id: number;
  video_id: string;
  video_url: string | null;
  school_slug: string;
  school_name: string | null;
  title: string | null;
  published_date: string | null;
  status: string;
  source_type: string | null;
  duration_seconds: number | null;
  word_count: number | null;
  quality_score: number | null;
}

export interface MeetingListResponse {
  meetings: Meeting[];
  pagination: Pagination;
}

// VoteSummary and FinancialSummary are projections of the full per-row types
// (Vote / Financial) declared further down. Deriving them via `Pick<>` keeps
// a single source of truth — renaming a field on Vote/Financial automatically
// flows through, and the compiler errors if a key here no longer exists.
// See refactor_candidates.md #2.
export type VoteSummary = Pick<
  Vote,
  | "vote_id"
  | "motion_text"
  | "vote_result_text"
  | "yes_count"
  | "no_count"
  | "passed"
  | "unanimous"
>;

export type FinancialSummary = Pick<
  Financial,
  | "item_id"
  | "action_type"
  | "category"
  | "vendor"
  | "amount"
  | "description"
>;

export interface PersonnelSummary {
  action_id: number;
  action_type: string | null;
  person_name: string | null;
  position: string | null;
  department: string | null;
  is_interim: boolean | null;
}

export interface TranscriptChunk {
  chunk_id: string;
  speaker: string | null;
  start_time: number | null;
  text: string;
  quality_score: number | null;
}

export interface MeetingOverview {
  meeting: Meeting;
  votes: VoteSummary[];
  financials: FinancialSummary[];
  personnel: PersonnelSummary[];
  key_chunks: TranscriptChunk[];
}

export interface TranscriptSegment {
  chunk_id: string;
  chunk_index: number | null;
  speaker: string | null;
  start_time: number | null;
  end_time: number | null;
  text: string;
  token_count: number | null;
  quality_score: number | null;
}

export interface MeetingTranscript {
  meeting: Meeting;
  segments: TranscriptSegment[];
}

// ── Votes ─────────────────────────────────────────────────────────────────────

export interface Vote {
  vote_id: number;
  school_slug: string;
  school_name: string | null;
  meeting_id: number;
  meeting_title: string | null;
  published_date: string | null;
  motion_text: string | null;
  vote_result_text: string | null;
  yes_count: number | null;
  no_count: number | null;
  abstain_count: number | null;
  passed: boolean | null;
  unanimous: boolean | null;
  moved_by: string | null;
  confidence: number;
}

export interface VoteListResponse {
  votes: Vote[];
  pagination: Pagination;
}

/** Page-level aggregate stats returned by /votes/summary. */
export interface VotesStats {
  total: number;
  passed: number;
  failed: number;
  unanimous: number;
  /** Fraction of decided motions (passed+failed) that passed; null if none decided. */
  pass_rate: number | null;
  /** Fraction of all motions that were unanimous; null if total=0. */
  unanimous_rate: number | null;
  top_movers: { name: string; cnt: number }[];
}

// ── Financials ────────────────────────────────────────────────────────────────

export interface Financial {
  item_id: number;
  school_slug: string;
  school_name: string | null;
  meeting_id: number;
  meeting_title: string | null;
  published_date: string | null;
  action_type: string | null;
  category: string | null;
  vendor: string | null;
  amount: number | null;
  description: string | null;
  confidence: number;
}

export interface FinancialListResponse {
  items: Financial[];
  pagination: Pagination;
}

/** Page-level aggregate stats returned by /financials/summary. */
export interface FinancialsStats {
  by_action_type: { action_type: string | null; cnt: number; total: number | null }[];
  top_vendors:    { vendor: string | null;      cnt: number; total: number | null }[];
  /**
   * The single largest financial item in the current scope. Surfaced so
   * trustees can spot outliers — a $20M building purchase says more about
   * priorities than a category roll-up. Click navigates to the meeting.
   */
  largest_item:   {
    item_id:        number;
    amount:         number | null;
    action_type:    string | null;
    vendor:         string | null;
    description:    string | null;
    meeting_id:     number;
    published_date: string | null;
    meeting_title:  string | null;
    school_slug:    string;
    school_name:    string | null;
  } | null;
}

// ── Insights ──────────────────────────────────────────────────────────────────

export interface InsightCell {
  insight_id: string;
  school_slug: string;
  school_name: string;
  theme_key: string;
  theme_label: string;
  label: string;
  action_type: string;
  meeting_count: number;
  confidence: number;
  has_detail: boolean;
}

export interface ThemeRow {
  theme_key: string;
  theme_label: string;
  /**
   * Cells keyed by school slug. Each cell is an array of insights —
   * a single (theme × school) pair can surface multiple related
   * initiatives (e.g., HCC × Academic Affairs = "AI & Robotics AAS",
   * "Dual credit growth", "Baccalaureate plan"). Empty array means
   * no signal detected for that pair.
   */
  cells: Record<string, InsightCell[]>;
}

export interface InsightMatrix {
  school_slugs: string[];
  school_names: Record<string, string>;
  themes: ThemeRow[];
  generated_at: string;
}

export interface EvidenceChunk {
  text: string;
  speaker: string | null;
  timestamp_sec: number | null;
  meeting_title: string | null;
  meeting_date: string | null;
  meeting_id: number | null;
  score: number | null;
}

export interface SupportingMeeting {
  meeting_id: number;
  title: string | null;
  date: string | null;
  school_slug: string;
}

export interface PeerCell {
  school_slug: string;
  school_name: string;
  label: string;
  action_type: string;
  insight_id: string;
}

export interface InsightDetail {
  insight_id: string;
  school_slug: string;
  school_name: string;
  theme_key: string;
  theme_label: string;
  label: string;
  action_type: string;
  summary: string;
  why_it_appears: string;
  confidence: number;
  supporting_meetings: SupportingMeeting[];
  evidence: EvidenceChunk[];
  related_votes: VoteSummary[];
  related_financials: FinancialSummary[];
  related_personnel: PersonnelSummary[];
  peer_cells: PeerCell[];
}

// ── Ask ───────────────────────────────────────────────────────────────────────

export interface Citation {
  type: string;           // "sql" | "rag"
  index: number | null;
  label: string | null;   // SQL citations
  title: string | null;   // RAG citations
  date: string | null;
  speaker: string | null;
  chunk_id: string | null;
  meeting_id: number | null;
  score: number | null;
}

export interface AskResponse {
  answer: string;
  route: string;          // "sql" | "rag" | "hybrid" | "compare" | "latest_meeting" | "none"
  citations: Citation[];
  model: string;
  elapsed_sec: number;

  // Present when route === "latest_meeting": the resolved meeting context.
  meeting_id?: number | null;
  meeting_title?: string | null;
  meeting_date?: string | null;
  school_slug?: string | null;
  school_name?: string | null;
}
