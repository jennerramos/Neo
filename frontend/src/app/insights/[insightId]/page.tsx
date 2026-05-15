import { fetchInsightDetail } from "@/lib/api";
import { actionTypeColor, cn } from "@/lib/utils";
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
  } catch {
    notFound();
  }

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
          <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
            {detail.theme_label}
          </span>
          <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
            {Math.round(detail.confidence * 100)}% confidence
          </span>
        </div>

        <h1 className="mt-4 text-2xl font-bold text-slate-900">{detail.label}</h1>
        <p className="mt-1 text-slate-500">{detail.school_name}</p>
      </div>

      {/* Evidence (shared component with the drawer) */}
      <EvidencePanel detail={detail} variant="full" />
    </div>
  );
}
