"use client";
import { useState } from "react";
import type { PatternRow } from "@/types";
import { cn, fmtDate } from "@/lib/utils";

interface PatternBandProps {
  patterns: PatternRow[];
  /** How many to show before "+N more". */
  previewLimit?: number;
}

/** Signal type → what the row is actually counting, in trustee language. */
const TYPE_LABEL: Record<PatternRow["signal_type"], string> = {
  recurring_initiative: "Initiative",
  budget_trend: "Spending",
  personnel_trend: "Personnel",
};

const TYPE_STYLE: Record<PatternRow["signal_type"], string> = {
  recurring_initiative: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  budget_trend: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  personnel_trend: "bg-amber-50 text-amber-700 ring-amber-200",
};

/** "career_technical_education" → "Career technical education". */
function humanise(category: string): string {
  const words = (category ?? "").replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Uncategorised";
}

/**
 * Cross-college signals, above the matrix.
 *
 * These are the only claims Neo makes that span institutions, and they are
 * gated: `needs_review=false` means an initiative signal has measured outcomes
 * from at least two different colleges — "two colleges measured it", not "one
 * college reported two numbers". The caller passes min_schools=2 for the same
 * reason.
 *
 * The period is stated on the band rather than assumed. Signals are built over
 * the whole pilot corpus, which starts in 2024, while the matrix below covers
 * a rolling six months. Without the date range on every row, a pattern last
 * seen in 2024 would sit above the matrix reading as current business — the
 * defect the rolling window removed from the matrix itself.
 */
export default function PatternBand({ patterns, previewLimit = 6 }: PatternBandProps) {
  const [expanded, setExpanded] = useState(false);

  if (patterns.length === 0) return null;

  // Breadth first — the number of colleges is what makes a signal a pattern
  // rather than one institution's record. Meetings, then recency, break ties.
  const ranked = [...patterns].sort(
    (a, b) =>
      b.school_count - a.school_count ||
      b.meeting_count - a.meeting_count ||
      (b.last_observed_date ?? "").localeCompare(a.last_observed_date ?? "")
  );

  // Initiative signals span the most colleges, so a straight breadth ranking
  // fills the whole preview with them and the band reads as though spending
  // and personnel are not tracked. Lead with the strongest of each kind, then
  // continue in breadth order.
  const strongestPerType = Object.values(TYPE_LABEL).length
    ? (Object.keys(TYPE_LABEL) as PatternRow["signal_type"][])
        .map((t) => ranked.find((p) => p.signal_type === t))
        .filter((p): p is PatternRow => Boolean(p))
    : [];
  const seeded = [
    ...strongestPerType,
    ...ranked.filter((p) => !strongestPerType.includes(p)),
  ];

  const hidden = Math.max(0, seeded.length - previewLimit);
  const visible = expanded ? seeded : seeded.slice(0, previewLimit);

  const earliest = ranked
    .map((p) => p.first_observed_date)
    .filter((d): d is string => Boolean(d))
    .sort()[0];

  return (
    <section aria-labelledby="pattern-band-heading" className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="pattern-band-heading" className="text-sm font-semibold text-slate-800">
          Recurring across colleges
        </h2>
        <p className="text-xs text-slate-500">
          {ranked.length} signals seen at two or more institutions
          {earliest && <> · since {fmtDate(earliest)}</>} · a longer view than the
          matrix below
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {visible.map((p) => (
          <div
            key={p.signal_id}
            className="rounded-lg border border-slate-200 bg-white px-3 py-2.5 shadow-sm"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-medium leading-snug text-slate-800">
                {humanise(p.category)}
              </span>
              <span
                className={cn(
                  "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ring-inset",
                  TYPE_STYLE[p.signal_type]
                )}
              >
                {TYPE_LABEL[p.signal_type]}
              </span>
            </div>

            <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-slate-500">
              <span className="font-semibold tabular-nums text-slate-700">
                {p.school_count} colleges
              </span>
              <span aria-hidden>·</span>
              <span className="tabular-nums">
                {p.meeting_count} meeting{p.meeting_count === 1 ? "" : "s"}
              </span>
            </div>

            <p className="mt-0.5 text-[11px] tabular-nums text-slate-400">
              {fmtDate(p.first_observed_date)} – {fmtDate(p.last_observed_date)}
            </p>
          </div>
        ))}
      </div>

      {hidden > 0 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="text-xs font-medium text-indigo-600 underline-offset-2 hover:underline"
        >
          {expanded ? "Show fewer signals" : `Show ${hidden} more signal${hidden === 1 ? "" : "s"}`}
        </button>
      )}
    </section>
  );
}
