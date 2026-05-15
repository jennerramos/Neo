import Link from "next/link";
import type { InsightDetail } from "@/types";
import { actionTypeColor, cn, fmtCurrency, fmtDate } from "@/lib/utils";

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
      {/* Summary + why it appears */}
      <Section title="Summary" compact={compact}>
        <p className={cn("leading-relaxed", compact ? "text-slate-700" : "text-base text-slate-700")}>
          {detail.summary}
        </p>
        {detail.why_it_appears && (
          <p className={cn("mt-2 italic text-slate-400", compact ? "text-xs" : "text-sm")}>
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
                key={i}
                className={compact
                  ? "rounded-lg bg-slate-50 p-3"
                  : "card px-5 py-4"}
              >
                <blockquote className={compact
                  ? "text-slate-700 leading-relaxed"
                  : "border-l-4 border-indigo-300 pl-4 text-slate-700 leading-relaxed italic"}>
                  &ldquo;{ev.text}&rdquo;
                </blockquote>
                <p className="mt-1.5 text-xs text-slate-400">
                  {ev.speaker && <><span className="font-medium">{ev.speaker}</span> · </>}
                  {ev.meeting_title} · {fmtDate(ev.meeting_date)}
                  {!compact && ev.score != null && <> · {Math.round(ev.score * 100)}% confidence</>}
                </p>
              </div>
            ))}
          </div>
          {compact && detail.evidence.length > evidenceLimit && (
            <p className="mt-2 text-xs text-slate-400">
              +{detail.evidence.length - evidenceLimit} more on the full page
            </p>
          )}
        </Section>
      )}

      {/* Supporting meetings */}
      {detail.supporting_meetings.length > 0 && (
        <Section
          title={`Supporting Meetings (${detail.supporting_meetings.length})`}
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

      {/* Related votes + financials */}
      <div className={cn(!compact && detail.related_votes.length > 0 && detail.related_financials.length > 0 && "grid gap-6 sm:grid-cols-2")}>
        {detail.related_votes.length > 0 && (
          <Section title="Related Votes" compact={compact}>
            <div className={compact ? "space-y-2" : "card divide-y divide-slate-100"}>
              {detail.related_votes.slice(0, votesLimit).map((v) => (
                <div
                  key={v.vote_id}
                  className={compact ? "flex items-start gap-2" : "flex gap-3 px-4 py-3"}
                >
                  <span className={cn(
                    "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold leading-none",
                    v.passed ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-700"
                  )}>
                    {compact ? (v.passed ? "PASSED" : "FAILED") : v.passed ? "✓" : "✗"}
                  </span>
                  <p className="line-clamp-2 text-sm text-slate-700">{v.motion_text}</p>
                </div>
              ))}
            </div>
          </Section>
        )}

        {detail.related_financials.length > 0 && (
          <Section title="Related Financials" compact={compact}>
            <div className={compact ? "space-y-1.5" : "card divide-y divide-slate-100"}>
              {detail.related_financials.slice(0, financialsLimit).map((f) => (
                <div
                  key={f.item_id}
                  className={compact
                    ? "flex items-center justify-between"
                    : "flex items-center justify-between px-4 py-3"}
                >
                  <p className="line-clamp-1 flex-1 text-sm text-slate-700">
                    {f.description ?? f.vendor ?? f.category ?? "—"}
                  </p>
                  <span className="ml-3 shrink-0 text-sm font-semibold text-slate-900">
                    {fmtCurrency(f.amount)}
                  </span>
                </div>
              ))}
            </div>
          </Section>
        )}
      </div>

      {/* Related personnel (full variant only — compact rarely has room) */}
      {!compact && detail.related_personnel.length > 0 && (
        <Section title="Related Personnel" compact={false}>
          <div className="card divide-y divide-slate-100">
            {detail.related_personnel.slice(0, 8).map((p) => (
              <div key={p.action_id} className="flex items-center justify-between px-4 py-3 text-sm">
                <div className="min-w-0">
                  <p className="font-medium text-slate-800 truncate">{p.person_name ?? "—"}</p>
                  <p className="text-xs text-slate-400 truncate">
                    {p.position}{p.department && ` · ${p.department}`}
                  </p>
                </div>
                <span className="ml-3 shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-slate-600">
                  {p.action_type}
                </span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Peer cells */}
      {detail.peer_cells.length > 0 && (
        <Section title="Same Theme at Other Colleges" compact={compact}>
          <div className={compact ? "space-y-2" : "grid gap-3 sm:grid-cols-2"}>
            {detail.peer_cells.map((p) => (
              <Link
                key={p.insight_id}
                href={`/insights/${encodeURIComponent(p.insight_id)}`}
                className={compact
                  ? "flex items-start gap-3 rounded-lg border border-slate-100 p-3 transition hover:border-indigo-200"
                  : "card px-4 py-3 transition hover:border-indigo-200 hover:shadow-sm"}
              >
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{p.school_name}</p>
                  <p className={cn("mt-0.5 line-clamp-2 text-slate-800", compact ? "text-sm" : "text-sm font-medium")}>
                    {p.label}
                  </p>
                  {!compact && (
                    <span className={cn(
                      "mt-2 inline-block rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase",
                      actionTypeColor(p.action_type)
                    )}>
                      {p.action_type}
                    </span>
                  )}
                </div>
                {compact && (
                  <span className={cn(
                    "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase",
                    actionTypeColor(p.action_type)
                  )}>
                    {p.action_type}
                  </span>
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
      <h3 className={cn(
        "mb-2 font-semibold uppercase tracking-widest text-slate-400",
        compact ? "text-[10px]" : "text-xs"
      )}>
        {title}
      </h3>
      <div>{children}</div>
    </section>
  );
}
