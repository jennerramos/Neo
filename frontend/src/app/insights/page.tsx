import { fetchInsightMatrix } from "@/lib/api";
import InsightMatrix from "@/components/matrix/InsightMatrix";
import { fmtDate } from "@/lib/utils";

export const dynamic = "force-dynamic";

export default async function InsightsPage() {
  let matrix;
  try {
    matrix = await fetchInsightMatrix();
  } catch {
    return (
      <div className="card flex flex-col items-center justify-center gap-2 py-24 text-center">
        <p className="text-sm font-medium text-slate-700">Insights aren&rsquo;t available right now.</p>
        <p className="max-w-sm text-sm text-slate-500">
          The board-meeting data service didn&rsquo;t respond. Try again in a moment — if it keeps
          happening, let your Neo administrator know.
        </p>
      </div>
    );
  }

  const thin = matrix.coverage.filter((c) => c.meeting_count === 0);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">Cross-College Insights</h1>
        <p className="page-subtitle">
          What eight community-college boards have taken up in meetings held in the last{" "}
          {matrix.window_months} months. Rows are strategic themes · Columns are institutions.
          Click any item to read the transcript evidence behind it.
        </p>
      </div>

      {/* Coverage and window — the page used to state neither, so a reader
          could not tell what period it covered or how much was left out. */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border border-slate-200 bg-slate-50/70 px-4 py-3 text-xs text-slate-600">
        <span>
          <span className="font-semibold text-slate-800">Period covered</span>{" "}
          {fmtDate(matrix.window_start)} – {fmtDate(matrix.window_end)}
        </span>
        <span>
          <span className="font-semibold text-slate-800">Items recorded</span>{" "}
          {matrix.available_count}
        </span>
        <span>
          <span className="font-semibold text-slate-800">Meetings analysed</span>{" "}
          {matrix.coverage.reduce((n, c) => n + c.meeting_count, 0)}
        </span>
      </div>

      <InsightMatrix data={matrix} />

      {/* Legend. Evidence strength first — it is what tells a trustee how much
          weight an item carries. Action type covers all seven values the
          extractor emits; it previously listed five, so "other" and
          "cancelled" appeared on the page with no key. */}
      <div className="space-y-3 pt-2 text-xs text-slate-600">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="font-medium uppercase tracking-wider text-slate-400">
            Evidence strength
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
            Measured result reported
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full border-[1.5px] border-blue-500" aria-hidden />
            Board action recorded
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2" aria-hidden />
            Mentioned in discussion
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="font-medium uppercase tracking-wider text-slate-400">Action type</span>
          {[
            { label: "Approved", dot: "bg-emerald-400" },
            { label: "Launched", dot: "bg-blue-400" },
            { label: "Proposed", dot: "bg-amber-400" },
            { label: "Discussed", dot: "bg-slate-300" },
            { label: "Continued", dot: "bg-purple-400" },
            { label: "Cancelled", dot: "bg-red-400" },
            { label: "Other", dot: "bg-stone-400" },
          ].map(({ label, dot }) => (
            <span key={label} className="inline-flex items-center gap-1.5">
              <span className={`h-2.5 w-2.5 rounded-sm ${dot}`} aria-hidden />
              {label}
            </span>
          ))}
        </div>
      </div>

      {/* Per-college coverage. A short column is usually a corpus fact, not a
          governance one — Mt. SAC showing three items next to Dallas's twenty
          means we have processed two of its recordings, not that its board
          did less. */}
      <details className="rounded-lg border border-slate-200 bg-white">
        <summary className="cursor-pointer list-none px-4 py-3 text-xs font-medium text-slate-600 hover:text-indigo-700">
          How much material each college contributed →
        </summary>
        <div className="overflow-x-auto border-t border-slate-100">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-400">
                <th className="px-4 py-2 text-left font-medium">College</th>
                <th className="px-4 py-2 text-right font-medium">Meetings analysed</th>
                <th className="px-4 py-2 text-right font-medium">Items</th>
                <th className="px-4 py-2 text-right font-medium">Most recent meeting</th>
              </tr>
            </thead>
            <tbody>
              {matrix.coverage.map((c) => (
                <tr key={c.school_slug} className="border-t border-slate-100">
                  <td className="px-4 py-2 text-slate-700">{c.school_name}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-600">
                    {c.meeting_count}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-slate-600">
                    {c.insight_count}
                  </td>
                  <td className="px-4 py-2 text-right text-slate-500">
                    {c.latest_meeting_date ? fmtDate(c.latest_meeting_date) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {thin.length > 0 && (
          <p className="border-t border-slate-100 px-4 py-2 text-[11px] text-slate-500">
            No meetings analysed in this period for{" "}
            {thin.map((c) => c.school_name).join(", ")}. Blank cells for these colleges mean
            missing recordings, not inactivity.
          </p>
        )}
      </details>
    </div>
  );
}
