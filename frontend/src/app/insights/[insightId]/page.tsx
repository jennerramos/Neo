import { fetchInsightDetail } from "@/lib/api";
import { actionTypeColor, cn, evidenceLevelColor, fmtDate } from "@/lib/utils";
import Link from "next/link";
import { notFound } from "next/navigation";
import EvidencePanel from "@/components/insights/EvidencePanel";

export const dynamic = "force-dynamic";

export default async function InsightDetailPage({
  params,
}: {
  params: { insightId: string };
}) {
  let detail;
  try {
    detail = await fetchInsightDetail(decodeURIComponent(params.insightId));
  } catch (err) {
    // The API now 404s an id it cannot resolve rather than substituting the
    // theme's top item, so a 404 means the insight genuinely is not in the
    // current window — a stale bookmark, or a fabricated URL.
    //
    // Anything else is the backend failing. Catching both as notFound() would
    // tell a trustee the record does not exist when in fact we could not
    // reach it, so non-404s are rethrown for the error boundary.
    if (err instanceof Error && /^API 404:/.test(err.message)) notFound();
    throw err;
  }

  const dateRange =
    detail.first_meeting_date && detail.last_meeting_date
      ? detail.first_meeting_date === detail.last_meeting_date
        ? fmtDate(detail.last_meeting_date)
        : `${fmtDate(detail.first_meeting_date)} – ${fmtDate(detail.last_meeting_date)}`
      : null;

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-slate-400">
        <Link href="/insights" className="hover:text-indigo-600">Insights</Link>
        <span>/</span>
        <span className="text-slate-600">{detail.theme_label}</span>
        <span>/</span>
        <span className="max-w-[240px] truncate font-medium text-slate-900">{detail.label}</span>
      </nav>

      {/* Header card */}
      <div className="card px-8 py-6">
        <div className="flex flex-wrap items-start gap-3">
          <span className={cn(
            "rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide",
            actionTypeColor(detail.action_type)
          )}>
            {detail.action_type}
          </span>
          {/* Was "{Math.round(confidence * 100)}% confidence". That number was
              extraction form-completeness, so the badge promised a certainty
              nothing behind it measured. */}
          <span className={cn(
            "rounded-full px-3 py-1 text-xs font-semibold",
            evidenceLevelColor(detail.evidence_level)
          )}>
            {detail.evidence_label}
          </span>
          <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
            {detail.theme_label}
          </span>
        </div>

        <h1 className="mt-4 text-2xl font-bold text-slate-900">{detail.label}</h1>
        <p className="mt-1 text-slate-500">
          {detail.school_name}
          {dateRange && <> · {dateRange}</>}
          {detail.meeting_count > 1 && <> · {detail.meeting_count} meetings</>}
        </p>
        <p className="mt-3 text-sm text-slate-500">{detail.evidence_note}</p>
      </div>

      {/* Evidence (shared component with the drawer) */}
      <EvidencePanel detail={detail} variant="full" />
    </div>
  );
}
