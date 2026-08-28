import Link from "next/link";
import type { InsightDetail } from "@/types";
import {
  actionTypeBar,
  actionTypeColor,
  cn,
  evidenceLevelColor,
  fmtCurrency,
  fmtDate,
} from "@/lib/utils";
import SourceLink from "@/components/ui/SourceLink";

export type EvidenceVariant = "compact" | "full";

interface EvidencePanelProps {
  detail: InsightDetail;
  variant?: EvidenceVariant;
  /** In compact mode, the drawer shows a "Full page" CTA at the bottom. */
  showFullPageCta?: boolean;
}

/**
 * Single source of truth for rendering an insight's evidence.
 * Used by InsightDrawer (variant="compact") and
 * /insights/[insightId]/page.tsx (variant="full"). Keeping both
 * surfaces on one component prevents content drift.
 */
export default function EvidencePanel({
  detail,
  variant = "full",
  showFullPageCta = false,
}: EvidencePanelProps) {
  const compact = variant === "compact";
  const evidenceLimit = compact ? 3 : detail.evidence.length;
  const votesLimit = compact ? 4 : 6;
  const financialsLimit = compact ? 4 : 6;

  return (
    <div className={cn("space-y-6", compact && "text-sm")}>
      {/* What the record says — the four evidence levels, kept apart.
          The extractor separates observed / rationale / claimed / measured
          deliberately, and the API used to concatenate them into one
          paragraph. A college's forecast then read like a result. */}
      <Section title="What the record says" compact={compact}>
        <dl className="space-y-3">
          <Level
            term="What the board did"
            value={detail.observed_action ?? detail.description ?? detail.summary}
            compact={compact}
          />
          <Level
            term="Why they said they did it"
            value={detail.stated_rationale}
            compact={compact}
          />
          <Level
            term="What they expect"
            value={detail.claimed_outcome}
            compact={compact}
            hint="A prediction stated by the college — not a result."
          />
          <Level
            term="What was measured"
            value={detail.measured_outcome}
            compact={compact}
            hint="Reported by college staff in the meeting."
            emphasise
          />
        </dl>
        {detail.why_it_appears && (
          <p className={cn("mt-3 text-slate-500", compact ? "text-xs" : "text-sm")}>
            {detail.why_it_appears}
          </p>
        )}
      </Section>

      {/* Transcript evidence */}
      {detail.evidence.length > 0 && (
        <Section title="Transcript Evidence" compact={compact}>
          <div className="space-y-2">
            {detail.evidence.slice(0, evidenceLimit).map((ev, i) => (
              <div
                key={ev.chunk_id ? `${ev.chunk_id}-${i}` : i}
                className={compact ? "rounded-lg bg-slate-50 p-3" : "card px-5 py-4"}
              >
                <blockquote
                  className={
                    compact
                      ? "text-slate-700 leading-relaxed"
                      : "border-l-4 border-indigo-300 pl-4 text-slate-700 leading-relaxed italic"
                  }
                >
                  &ldquo;{ev.text}&rdquo;
                </blockquote>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
                  {ev.speaker && <span className="font-medium">{ev.speaker}</span>}
                  <span>
                    {ev.meeting_title} · {fmtDate(ev.meeting_date)}
                  </span>
                  {/* The quote was located character-for-character in a named
                      chunk. That is the page's strongest trust signal and it
                      used to ship in the payload without ever being shown. */}
                  {ev.verified && (
                    <span
                      className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-700"
                      title="This quote was matched word-for-word against the meeting transcript."
                    >
                      <svg
                        viewBox="0 0 16 16"
                        className="h-3 w-3"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        aria-hidden="true"
                      >
                        <path d="M3 8.5l3.5 3.5L13 5" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      Verified
                    </span>
                  )}
                  {ev.meeting_id != null && ev.chunk_id && (
                    <SourceLink meetingId={ev.meeting_id} chunkIds={[ev.chunk_id]} />
                  )}
                </div>
              </div>
            ))}
          </div>
          {compact && detail.evidence.length > evidenceLimit && (
            <p className="mt-2 text-xs text-slate-400">
              +{detail.evidence.length - evidenceLimit} more on the full page
            </p>
          )}
          <p className={cn("mt-2 text-slate-400", compact ? "text-[11px]" : "text-xs")}>
            Quotes come from automated transcription and may contain
            transcription errors. Open the source to check any wording.
          </p>
        </Section>
      )}

      {/* How the item moved. Only worth a section when there is a progression
          to show: with one meeting the card's own action state already says
          everything this would. */}
      {detail.timeline.length > 1 && (
        <Section title="How this progressed" compact={compact}>
          <ol className="relative space-y-2.5 pl-4">
            <span
              className="absolute bottom-1 left-[3px] top-1 w-px bg-slate-200"
              aria-hidden
            />
            {detail.timeline.map((step) => (
              <li key={step.meeting_id} className="relative">
                <span
                  className={cn(
                    "absolute -left-4 top-1 h-[7px] w-[7px] rounded-full ring-2 ring-white",
                    actionTypeBar(step.action_type)
                  )}
                  aria-hidden
                />
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="text-xs font-semibold capitalize text-slate-700">
                    {step.action_type}
                  </span>
                  <span className="text-xs tabular-nums text-slate-400">
                    {fmtDate(step.date)}
                  </span>
                </div>
                <Link
                  href={`/meetings/${step.meeting_id}`}
                  className="text-xs text-slate-500 hover:text-indigo-700 hover:underline"
                >
                  {step.title ?? `Meeting ${step.meeting_id}`}
                </Link>
              </li>
            ))}
          </ol>
        </Section>
      )}

      {/* Supporting meetings — exactly the meetings that produced this insight */}
      {detail.supporting_meetings.length > 0 && (
        <Section
          title={`Meetings this comes from (${detail.supporting_meetings.length})`}
          compact={compact}
        >
          {compact ? (
            <ul className="space-y-1">
              {detail.supporting_meetings.map((m) => (
                <li key={m.meeting_id} className="flex items-center justify-between">
                  <Link
                    href={`/meetings/${m.meeting_id}`}
                    className="truncate max-w-[280px] text-indigo-600 hover:underline"
                  >
                    {m.title ?? `Meeting #${m.meeting_id}`}
                  </Link>
                  <span className="ml-2 shrink-0 text-xs text-slate-400">{fmtDate(m.date)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="card divide-y divide-slate-100">
              {detail.supporting_meetings.map((m) => (
                <Link
                  key={m.meeting_id}
                  href={`/meetings/${m.meeting_id}`}
                  className="flex items-center justify-between px-5 py-3 hover:bg-slate-50"
                >
                  <span className="text-sm text-indigo-600 hover:underline">
                    {m.title ?? `Meeting #${m.meeting_id}`}
                  </span>
                  <span className="ml-4 shrink-0 text-xs text-slate-400">{fmtDate(m.date)}</span>
                </Link>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* Votes and dollar figures quoted from the same passage.
          Bound on transcript-chunk overlap. The previous version listed the
          whole theme's spending sorted by amount, which put a $1.048bn
          all-funds budget beside a home-visit partnership on most pages.
          Empty is the normal case and the section simply disappears. */}
      {(detail.related_votes.length > 0 || detail.related_financials.length > 0) && (
        <div
          className={cn(
            !compact &&
              detail.related_votes.length > 0 &&
              detail.related_financials.length > 0 &&
              "grid gap-6 sm:grid-cols-2"
          )}
        >
          {detail.related_votes.length > 0 && (
            <Section title="Votes in the same passage" compact={compact}>
              <div className={compact ? "space-y-2" : "card divide-y divide-slate-100"}>
                {detail.related_votes.slice(0, votesLimit).map((v) => (
                  <div
                    key={v.vote_id}
                    className={compact ? "flex items-start gap-2" : "flex gap-3 px-4 py-3"}
                  >
                    <span
                      className={cn(
                        "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold leading-none",
                        v.passed ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-700"
                      )}
                    >
                      {compact ? (v.passed ? "PASSED" : "FAILED") : v.passed ? "✓" : "✗"}
                    </span>
                    <p className="line-clamp-2 flex-1 text-sm text-slate-700">{v.motion_text}</p>
                    <SourceLink meetingId={v.meeting_id} chunkIds={v.chunk_ids} />
                  </div>
                ))}
              </div>
            </Section>
          )}

          {detail.related_financials.length > 0 && (
            <Section title="Amounts cited in the same passage" compact={compact}>
              <div className={compact ? "space-y-1.5" : "card divide-y divide-slate-100"}>
                {detail.related_financials.slice(0, financialsLimit).map((f) => (
                  <div
                    key={f.item_id}
                    className={
                      compact
                        ? "flex items-center justify-between gap-2"
                        : "flex items-center justify-between gap-2 px-4 py-3"
                    }
                  >
                    <p className="line-clamp-1 flex-1 text-sm text-slate-700">
                      {f.description ?? f.vendor ?? f.category ?? "—"}
                    </p>
                    <span className="shrink-0 text-sm font-semibold text-slate-900">
                      {fmtCurrency(f.amount)}
                    </span>
                    <SourceLink meetingId={f.meeting_id} chunkIds={f.chunk_ids} />
                  </div>
                ))}
              </div>
            </Section>
          )}
        </div>
      )}

      {/* Ask Neo about this. The question names the college and the item, so
          the retriever gets the two things that scope it — a bare "tell me
          about this" would be routed against the whole corpus. The school is
          passed separately as the request filter, not just in the prose. */}
      <div className="pt-1">
        <Link
          href={`/?q=${encodeURIComponent(
            `What has ${detail.school_name} said about ${detail.label} in its board meetings?`
          )}&school=${encodeURIComponent(detail.school_slug)}`}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50/60 px-3 py-1.5 font-medium text-indigo-700 transition hover:border-indigo-300 hover:bg-indigo-100",
            compact ? "text-xs" : "text-sm"
          )}
        >
          Ask Neo about this
          <span aria-hidden>&rarr;</span>
        </Link>
      </div>

      {/* Peer cells */}
      {detail.peer_cells.length > 0 && (
        <Section title="Same Theme at Other Colleges" compact={compact}>
          <p className={cn("mb-2 text-slate-400", compact ? "text-[11px]" : "text-xs")}>
            The strongest item each college recorded under this theme in the same
            period. These are separate initiatives, not the same one compared.
          </p>
          <div className={compact ? "space-y-2" : "grid gap-3 sm:grid-cols-2"}>
            {detail.peer_cells.map((p) => (
              <Link
                key={p.insight_id}
                href={`/insights/${encodeURIComponent(p.insight_id)}`}
                className={
                  compact
                    ? "flex items-start gap-3 rounded-lg border border-slate-100 p-3 transition hover:border-indigo-200"
                    : "card px-4 py-3 transition hover:border-indigo-200 hover:shadow-sm"
                }
              >
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    {p.school_name}
                  </p>
                  <p
                    className={cn(
                      "mt-0.5 line-clamp-2 text-slate-800",
                      compact ? "text-sm" : "text-sm font-medium"
                    )}
                  >
                    {p.label}
                  </p>
                  {!compact && (
                    <span className="mt-2 flex flex-wrap gap-1.5">
                      <Badge className={actionTypeColor(p.action_type)}>{p.action_type}</Badge>
                      <Badge className={evidenceLevelColor(p.evidence_level)}>
                        {p.evidence_label}
                      </Badge>
                    </span>
                  )}
                </div>
                {compact && (
                  <Badge className={cn("shrink-0", actionTypeColor(p.action_type))}>
                    {p.action_type}
                  </Badge>
                )}
              </Link>
            ))}
          </div>
        </Section>
      )}

      {/* Drawer-only CTA to open the full page */}
      {showFullPageCta && (
        <div className="pt-2">
          <Link
            href={`/insights/${encodeURIComponent(detail.insight_id)}`}
            className="inline-flex w-full items-center justify-center rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
          >
            View full detail page →
          </Link>
        </div>
      )}
    </div>
  );
}

/** One of the four evidence levels. Renders nothing when the level is absent. */
function Level({
  term,
  value,
  compact,
  hint,
  emphasise = false,
}: {
  term: string;
  value: string | null | undefined;
  compact: boolean;
  hint?: string;
  emphasise?: boolean;
}) {
  if (!value) return null;
  return (
    <div>
      <dt
        className={cn(
          "font-semibold uppercase tracking-wide",
          compact ? "text-[10px]" : "text-[11px]",
          emphasise ? "text-emerald-700" : "text-slate-400"
        )}
      >
        {term}
      </dt>
      <dd
        className={cn(
          "mt-0.5 leading-relaxed text-slate-700",
          compact ? "text-sm" : "text-base",
          emphasise && "rounded-md bg-emerald-50/70 px-3 py-2 text-emerald-900"
        )}
      >
        {value}
        {hint && (
          <span className={cn("mt-0.5 block text-slate-400", compact ? "text-[11px]" : "text-xs")}>
            {hint}
          </span>
        )}
      </dd>
    </div>
  );
}

function Badge({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "inline-block rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase",
        className
      )}
    >
      {children}
    </span>
  );
}

function Section({
  title,
  compact,
  children,
}: {
  title: string;
  compact: boolean;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h3
        className={cn(
          "mb-2 font-semibold uppercase tracking-widest text-slate-400",
          compact ? "text-[10px]" : "text-xs"
        )}
      >
        {title}
      </h3>
      <div>{children}</div>
    </section>
  );
}
