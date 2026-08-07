"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { askNeoStream } from "@/lib/api";
import type { AskResponse, Citation } from "@/types";
import { cn, fmtDate } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";

/**
 * Per-tab persistence for the last Ask result. Solves the back-button
 * problem: trustees click a [N] citation, navigate to the meeting transcript
 * to verify, then hit back — without this, the answer panel they were
 * reading is gone (component remounted with empty state).
 *
 * sessionStorage (not localStorage): cleared when the tab closes, scoped
 * per-tab so two parallel Ask sessions don't trample each other.
 */
const ASK_STATE_KEY = "neo:ask:state";

interface PersistedAskState {
  query: string;
  school: string;
  result: AskResponse;
  /** ISO timestamp — surfaced when restoring so users see when this answer was generated. */
  savedAt: string;
}

function loadAskState(): PersistedAskState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(ASK_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PersistedAskState;
    // Defensive: shape check so a future schema change doesn't crash hydration.
    if (!parsed?.result?.answer || !Array.isArray(parsed.result.citations)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveAskState(state: PersistedAskState): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(ASK_STATE_KEY, JSON.stringify(state));
  } catch {
    /* sessionStorage unavailable (private mode, quota) — silent best-effort */
  }
}

function clearAskState(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(ASK_STATE_KEY);
  } catch {
    /* see saveAskState */
  }
}

/**
 * Highlight the citation card matching `index` for ~1.2s. We attach a class
 * directly to the DOM node rather than threading state through React because
 * the card list is short-lived (lives only with the result) and a transient
 * highlight has no impact on app state.
 */
function flashCitation(index: number) {
  const el = document.getElementById(`cite-${index}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("ring-2", "ring-indigo-400", "ring-offset-2");
  window.setTimeout(() => {
    el.classList.remove("ring-2", "ring-indigo-400", "ring-offset-2");
  }, 1200);
}

/**
 * Parse the answer text and emit a mix of strings + clickable [N] badges.
 *
 * Handles three LLM marker shapes:
 *   [3]            → one badge for source 3
 *   [3][7]         → two adjacent badges
 *   [3, 7] / [3,7] → two badges (LLM sometimes combines)
 *
 * Markers that point past the available citation count are still rendered
 * (so we don't hide content) but as muted/non-clickable badges.
 */
function renderAnswerWithMarkers(answer: string, maxIndex: number): React.ReactNode[] {
  // Match either [N] or [N, M, ...] — capture the inside so we can split.
  const re = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
  const out: React.ReactNode[] = [];
  let last = 0;
  let key = 0;
  // Use exec() in a loop instead of matchAll() iteration, which would need
  // downlevelIteration enabled for the project's current TS target.
  let m: RegExpExecArray | null;
  while ((m = re.exec(answer)) !== null) {
    const start = m.index;
    if (start > last) {
      out.push(answer.slice(last, start));
    }
    const nums: number[] = m[1]
      .split(",")
      .map((s: string) => parseInt(s.trim(), 10))
      .filter((n: number) => Number.isFinite(n));
    for (const n of nums) {
      const valid = n >= 1 && n <= maxIndex;
      out.push(
        <button
          key={`m-${key++}`}
          type="button"
          onClick={valid ? () => flashCitation(n) : undefined}
          aria-label={`Source ${n}`}
          className={cn(
            "inline-flex items-center justify-center mx-0.5 align-baseline",
            "rounded px-1.5 text-[11px] font-bold leading-tight",
            valid
              ? "bg-indigo-100 text-indigo-700 hover:bg-indigo-600 hover:text-white cursor-pointer transition-colors"
              : "bg-slate-100 text-slate-400 cursor-default",
          )}
        >
          {n}
        </button>
      );
    }
    last = start + m[0].length;
  }
  if (last < answer.length) {
    out.push(answer.slice(last));
  }
  return out;
}

const ROUTE_COLORS: Record<string, string> = {
  sql:             "bg-blue-100 text-blue-800",
  rag:             "bg-violet-100 text-violet-800",
  hybrid:          "bg-teal-100 text-teal-800",
  compare:         "bg-amber-100 text-amber-800",
  latest_meeting:  "bg-indigo-100 text-indigo-800",
  none:            "bg-slate-100 text-slate-600",
};

const ROUTE_LABELS: Record<string, string> = {
  latest_meeting: "latest meeting",
};

const DEFAULT_SUGGESTED = [
  "What happened in the most recent HCC meeting?",
  "What votes did HCC pass in the last 6 months?",
  "How much did Lone Star College approve in contracts this year?",
  "What is HCC doing in AI and technology?",
  "Compare HCC and Lone Star on workforce development initiatives.",
];

export interface AskBoxProps {
  /** Show the "Ask Neo" eyebrow label + centered hero layout. Use on the home page. */
  hero?: boolean;
  /** Override suggested prompt chips. */
  suggested?: string[];
  /** Pass known schools here once wired; falls back to a minimal list. */
  schools?: { slug: string; name: string }[];
}

export default function AskBox({ hero = false, suggested = DEFAULT_SUGGESTED, schools }: AskBoxProps) {
  const [query, setQuery] = useState("");
  const [school, setSchool] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Filled from sessionStorage when the user comes back from a citation page. */
  const [restoredAt, setRestoredAt] = useState<string | null>(null);

  // Held so an in-flight stream can be cancelled if the user clicks a
  // suggestion again or hits reset. Not state — we don't need re-renders.
  const streamCtrlRef = useRef<AbortController | null>(null);

  const schoolOptions = schools ?? [
    { slug: "houston_city_college", name: "Houston City College" },
    { slug: "lone_star_college",    name: "Lone Star College" },
  ];

  // Hydrate from sessionStorage on mount. Runs in an effect (not at state
  // init) because sessionStorage is browser-only and Next.js renders this
  // component on the server first.
  useEffect(() => {
    const persisted = loadAskState();
    if (persisted) {
      setQuery(persisted.query);
      setSchool(persisted.school);
      setResult(persisted.result);
      setRestoredAt(persisted.savedAt);
    }
  }, []);

  // Cancel any in-flight stream on unmount.
  useEffect(() => () => streamCtrlRef.current?.abort(), []);

  function submit(q?: string) {
    const text = (q ?? query).trim();
    if (!text) return;

    // Cancel any prior in-flight stream — otherwise its onToken callbacks
    // would continue to mutate state after we've moved on.
    streamCtrlRef.current?.abort();

    setQuery(text);
    setLoading(true);
    setError(null);
    setRestoredAt(null);
    // Seed a partial result so the answer panel appears immediately;
    // meta arrives next, tokens append after that.
    setResult({
      answer:      "",
      route:       "",
      citations:   [],
      model:       "",
      elapsed_sec: 0,
    });

    let accumulated = "";
    let meta: Partial<AskResponse> = {};

    streamCtrlRef.current = askNeoStream(
      { query: text, school_slug: school || undefined, top_k: 8 },
      {
        onMeta: (m) => {
          meta = m;
          setResult((prev) => ({
            answer:        accumulated,
            route:         m.route ?? "",
            citations:     m.citations ?? [],
            model:         m.model ?? "",
            elapsed_sec:   prev?.elapsed_sec ?? 0,
            meeting_id:    m.meeting_id,
            meeting_title: m.meeting_title,
            meeting_date:  m.meeting_date,
            school_slug:   m.school_slug,
            school_name:   m.school_name,
          }));
        },
        onToken: (tok) => {
          accumulated += tok;
          setResult((prev) => (prev ? { ...prev, answer: accumulated } : prev));
        },
        onDone: (elapsedSec) => {
          setLoading(false);
          const finalRes: AskResponse = {
            answer:        accumulated,
            route:         (meta.route as string) ?? "",
            citations:     (meta.citations as Citation[]) ?? [],
            model:         (meta.model as string) ?? "",
            elapsed_sec:   elapsedSec,
            meeting_id:    meta.meeting_id,
            meeting_title: meta.meeting_title,
            meeting_date:  meta.meeting_date,
            school_slug:   meta.school_slug,
            school_name:   meta.school_name,
          };
          setResult(finalRes);
          saveAskState({
            query:   text,
            school,
            result:  finalRes,
            savedAt: new Date().toISOString(),
          });
        },
        onError: (msg) => {
          setLoading(false);
          setError(msg);
        },
      },
    );
  }

  function resetForAnother() {
    streamCtrlRef.current?.abort();
    setResult(null);
    setQuery("");
    setRestoredAt(null);
    clearAskState();
  }

  return (
    <div className={cn("space-y-6", hero && "mx-auto max-w-3xl")}>
      {hero && (
        <div className="text-center">
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-600">Ask Neo</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900 sm:text-4xl">
            Board intelligence, answered with evidence.
          </h1>
          <p className="mt-3 text-sm text-slate-500">
            Ask anything about meetings, votes, financials, or initiatives across tracked colleges.
          </p>
        </div>
      )}

      {/* Prompt */}
      <div className="card p-5 space-y-3">
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={3}
          placeholder="Ask a question about board meetings… (Enter to send, Shift+Enter for newline)"
          className="w-full resize-none rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-900 placeholder-slate-400 focus:border-indigo-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-indigo-200 transition"
        />
        <div className="flex items-center gap-3">
          <select
            value={school}
            onChange={(e) => setSchool(e.target.value)}
            className="form-select"
          >
            <option value="">All schools</option>
            {schoolOptions.map((s) => (
              <option key={s.slug} value={s.slug}>{s.name}</option>
            ))}
          </select>
          <button
            onClick={() => submit()}
            disabled={loading || !query.trim()}
            className={cn(
              "ml-auto rounded-lg px-5 py-2 text-sm font-semibold text-white transition-colors",
              loading || !query.trim()
                ? "bg-indigo-300 cursor-not-allowed"
                : "bg-indigo-600 hover:bg-indigo-700"
            )}
          >
            {loading ? "Thinking…" : "Ask Neo →"}
          </button>
        </div>
      </div>

      {/* Suggested prompts (only before a result is returned) */}
      {!result && !loading && (
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">Try asking</p>
          <div className="flex flex-wrap gap-2">
            {suggested.map((s) => (
              <button
                key={s}
                onClick={() => submit(s)}
                className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:border-indigo-300 hover:bg-indigo-50 hover:text-indigo-700 transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Initial spinner — only while streaming hasn't produced anything yet */}
      {loading && (!result || !result.answer) && (
        <div className="flex flex-col items-center gap-3 py-12 text-slate-400">
          <Spinner size={32} />
          <p className="text-sm">Neo is thinking…</p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</div>
      )}

      {/* Result — visible as soon as any answer text has streamed in */}
      {result && (result.answer || !loading) && (
        <div className="space-y-5">
          {/* Meta row */}
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
            {result.route && (
              <span className={cn(
                "rounded-full px-2.5 py-0.5 font-semibold uppercase tracking-wide",
                ROUTE_COLORS[result.route] ?? "bg-slate-100 text-slate-600"
              )}>
                {ROUTE_LABELS[result.route] ?? result.route}
              </span>
            )}
            {result.model && <><span>·</span><span>{result.model}</span></>}
            {result.elapsed_sec > 0 && <><span>·</span><span>{result.elapsed_sec}s</span></>}
            {loading && (
              <>
                <span>·</span>
                <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">
                  <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />
                  streaming
                </span>
              </>
            )}
            {/* When restoredAt is set we got here by hitting back from a citation
             *  page, not by hitting Submit — surface that gently so users don't
             *  wonder if they clicked the wrong button. */}
            {restoredAt && (
              <>
                <span>·</span>
                <span
                  className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-700"
                  title={`Generated ${new Date(restoredAt).toLocaleString()}`}
                >
                  restored from previous answer
                </span>
              </>
            )}
          </div>

          {/* Latest-meeting header — shows which specific meeting the answer is scoped to */}
          {result.route === "latest_meeting" && result.meeting_id && (
            <div className="flex items-center justify-between rounded-xl border border-indigo-200 bg-indigo-50/60 px-5 py-3">
              <div className="min-w-0">
                <p className="text-[10px] font-semibold uppercase tracking-widest text-indigo-600">
                  Scoped to the latest meeting
                </p>
                <p className="mt-0.5 truncate text-sm font-medium text-slate-800">
                  {result.meeting_title ?? `Meeting #${result.meeting_id}`}
                </p>
                <p className="mt-0.5 text-xs text-slate-500">
                  {result.school_name ?? result.school_slug}
                  {result.meeting_date && <> · {fmtDate(result.meeting_date)}</>}
                </p>
              </div>
              <Link
                href={`/meetings/${result.meeting_id}`}
                className="ml-4 shrink-0 rounded-lg border border-indigo-300 bg-white px-3 py-1.5 text-xs font-semibold text-indigo-700 transition hover:bg-indigo-600 hover:text-white"
              >
                View full meeting →
              </Link>
            </div>
          )}

          {/* Answer with inline [N] markers that scroll to the matching card */}
          <div className="card px-6 py-5">
            <div className="prose prose-sm max-w-none text-slate-800 leading-relaxed whitespace-pre-wrap">
              {renderAnswerWithMarkers(result.answer, result.citations.length)}
            </div>
          </div>

          {/* Citations */}
          {result.citations.length > 0 && (
            <div>
              <div className="mb-2 flex items-center gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-widest text-slate-400">
                  Sources ({result.citations.length})
                </h3>
                <span className="text-xs text-slate-300">— numbers match [N] in the answer above</span>
              </div>
              <div className="space-y-2">
                {result.citations.map((c, i) => (
                  <CitationCard key={i} citation={c} />
                ))}
              </div>
              <p className="mt-2 text-[11px] text-slate-400">
                Click any <span className="rounded bg-indigo-100 px-1 py-0.5 font-bold text-indigo-700">N</span> in the answer to jump to its source.
              </p>
            </div>
          )}

          {/* Ask another */}
          <button
            onClick={resetForAnother}
            className="text-sm text-indigo-600 hover:underline"
          >
            ← Ask another question
          </button>
        </div>
      )}
    </div>
  );
}

function CitationCard({ citation }: { citation: Citation }) {
  const isSql = citation.type === "sql";
  const hasLink = !isSql && citation.meeting_id != null;

  // Deep-link straight into the transcript at the cited chunk when we have one —
  // that closes the loop: "Neo said X [3]" → click → see the quoted paragraph.
  const href = hasLink
    ? (citation.chunk_id
        ? `/meetings/${citation.meeting_id}/transcript#chunk-${citation.chunk_id}`
        : `/meetings/${citation.meeting_id}`)
    : null;

  const linkLabel = citation.chunk_id ? "Jump to transcript →" : "View meeting →";

  const body = (
    <div
      id={citation.index != null ? `cite-${citation.index}` : undefined}
      className={cn(
        "flex gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 transition-all",
        hasLink && "hover:border-indigo-300 hover:bg-indigo-50/40"
      )}
    >
      {/* Big number badge — matches [N] in the answer text */}
      <div className="shrink-0 flex h-7 w-7 items-center justify-center rounded-full bg-indigo-100 text-sm font-bold text-indigo-700">
        {citation.index ?? "?"}
      </div>

      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-slate-800 leading-snug">
          {isSql
            ? (citation.label ?? "Structured board records")
            : (citation.title ?? "Board Meeting")}
        </p>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-slate-400">
          {!isSql && citation.date && <span>{citation.date}</span>}
          {citation.speaker && <><span>·</span><span className="font-medium text-slate-500">{citation.speaker}</span></>}
          {citation.score != null && (
            <><span>·</span><span>{Math.round(citation.score * 100)}% match</span></>
          )}
          {hasLink && (
            <><span>·</span><span className="text-indigo-600">{linkLabel}</span></>
          )}
          <span className={cn(
            "ml-auto rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
            isSql ? "bg-blue-100 text-blue-600" : "bg-violet-100 text-violet-600"
          )}>
            {citation.type}
          </span>
        </div>
      </div>
    </div>
  );

  if (href) {
    return <Link href={href} className="block">{body}</Link>;
  }
  return body;
}
