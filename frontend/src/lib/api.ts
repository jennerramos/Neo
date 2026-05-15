/**
 * Typed API client for Neo v2 backend.
 * All functions throw on non-2xx responses.
 */

import type {
  School,
  MeetingListResponse,
  MeetingOverview,
  MeetingTranscript,
  VoteListResponse,
  VotesStats,
  FinancialListResponse,
  FinancialsStats,
  InsightMatrix,
  InsightDetail,
  AskResponse,
} from "@/types";

// API base differs by execution context:
//
//   • Browser:  use the relative path "/api". Next.js rewrites it to the
//               real backend, so calls are same-origin → no CORS, works
//               identically on localhost / ngrok / deployed.
//
//   • Server:   server components (Insights page, /meetings/[id], etc.)
//               run inside Next.js. fetch("/api/…") has no host to resolve
//               against and 500s. So the server branch hits the backend
//               directly via NEO_API_TARGET (same env var the rewrite in
//               next.config.mjs uses, kept in sync intentionally).
//
// `typeof window === "undefined"` is true at SSR/RSC time, false in the
// client bundle. Next.js evaluates this module separately for each, so the
// branch resolves at the right side of the wire.
const BASE =
  typeof window === "undefined"
    ? (process.env.NEO_API_TARGET    ?? "http://localhost:8000")
    : (process.env.NEXT_PUBLIC_API_URL ?? "/api");

/**
 * Build a URL string. We don't use `new URL()` because the base may be
 * relative ("/api"), and `new URL("/api/votes")` throws when there's no
 * absolute base. String concat + URLSearchParams handles both modes.
 */
function buildUrl(path: string, params?: Record<string, string | number | boolean | undefined>): string {
  let url = `${BASE}${path}`;
  if (params) {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") {
        sp.set(k, String(v));
      }
    }
    const qs = sp.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const res = await fetch(buildUrl(path, params), { cache: "no-store" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

// ── Schools ───────────────────────────────────────────────────────────────────

export const fetchSchools = (): Promise<School[]> =>
  get("/schools");

// ── Meetings ──────────────────────────────────────────────────────────────────

export interface MeetingFilters {
  school?: string;
  date_from?: string;
  date_to?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export const fetchMeetings = (filters: MeetingFilters = {}): Promise<MeetingListResponse> =>
  get("/meetings", filters as Record<string, string | number | boolean | undefined>);

export const fetchMeeting = (id: number): Promise<MeetingOverview> =>
  get(`/meetings/${id}`);

export const fetchMeetingTranscript = (id: number): Promise<MeetingTranscript> =>
  get(`/meetings/${id}/transcript`);

// ── Votes ─────────────────────────────────────────────────────────────────────

export interface VoteFilters {
  school?: string;
  meeting_id?: number;
  passed?: boolean;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export const fetchVotes = (filters: VoteFilters = {}): Promise<VoteListResponse> =>
  get("/votes", filters as Record<string, string | number | boolean | undefined>);

export const fetchVotesSummary = (
  filters: { school?: string; date_from?: string; date_to?: string } = {},
): Promise<VotesStats> => get("/votes/summary", filters);

// ── Financials ────────────────────────────────────────────────────────────────

export interface FinancialFilters {
  school?: string;
  meeting_id?: number;
  action_type?: string;
  category?: string;
  amount_min?: number;
  amount_max?: number;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export const fetchFinancials = (filters: FinancialFilters = {}): Promise<FinancialListResponse> =>
  get("/financials", filters as Record<string, string | number | boolean | undefined>);

export const fetchFinancialsSummary = (
  filters: { school?: string; date_from?: string; date_to?: string } = {},
): Promise<FinancialsStats> => get("/financials/summary", filters);

// ── Insights ──────────────────────────────────────────────────────────────────

/**
 * Fetches the cross-college matrix and normalizes each cell into an
 * InsightCell[]. This lets the frontend treat multi-insight and
 * single-insight backends identically while the API evolves.
 */
export const fetchInsightMatrix = async (): Promise<InsightMatrix> => {
  const raw = await get<InsightMatrix>("/insights/matrix");
  return {
    ...raw,
    themes: raw.themes.map((row) => ({
      ...row,
      cells: Object.fromEntries(
        Object.entries(row.cells ?? {}).map(([slug, value]) => [slug, normalizeCell(value)])
      ),
    })),
  };
};

function normalizeCell(value: unknown): import("@/types").InsightCell[] {
  if (value == null) return [];
  if (Array.isArray(value)) return value as import("@/types").InsightCell[];
  // Legacy single-object shape.
  return [value as import("@/types").InsightCell];
}

export const fetchInsightDetail = (insightId: string): Promise<InsightDetail> =>
  get(`/insights/detail/${encodeURIComponent(insightId)}`);

// ── Ask ───────────────────────────────────────────────────────────────────────

export interface AskRequest {
  query: string;
  school_slug?: string;
  date_from?: string;
  date_to?: string;
  top_k?: number;
  force_route?: string;
}

export const askNeo = (req: AskRequest): Promise<AskResponse> =>
  post("/ask", req);

// ── Export helpers ────────────────────────────────────────────────────────────

export const exportVotesCsvUrl = (school?: string, dateFrom?: string, dateTo?: string) => {
  const p = new URLSearchParams();
  if (school) p.set("school", school);
  if (dateFrom) p.set("date_from", dateFrom);
  if (dateTo) p.set("date_to", dateTo);
  return `${BASE}/export/votes.csv?${p}`;
};

export const exportFinancialsCsvUrl = (school?: string, dateFrom?: string, dateTo?: string) => {
  const p = new URLSearchParams();
  if (school) p.set("school", school);
  if (dateFrom) p.set("date_from", dateFrom);
  if (dateTo) p.set("date_to", dateTo);
  return `${BASE}/export/financials.csv?${p}`;
};
