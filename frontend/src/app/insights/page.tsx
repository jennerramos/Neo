import { fetchInsightMatrix } from "@/lib/api";
import InsightMatrix from "@/components/matrix/InsightMatrix";

export const dynamic = "force-dynamic";

export default async function InsightsPage() {
  let matrix;
  try {
    matrix = await fetchInsightMatrix();
  } catch {
    return (
      <div className="card flex items-center justify-center py-24 text-slate-400">
        <p className="text-sm">Could not load insights — is the API running?</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">Cross-College Insights</h1>
        <p className="page-subtitle">
          Click any cell to see evidence, supporting meetings, and peer comparisons.
          Rows are strategic themes · Columns are institutions.
        </p>
      </div>

      <InsightMatrix data={matrix} />

      {/* Legend — dots use the same accent colours as the cell left-borders,
          so the legend reads as a key for the matrix itself. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 pt-2 text-xs text-slate-600">
        <span className="font-medium uppercase tracking-wider text-slate-400">Action type</span>
        {[
          { label: "Approved",  dot: "bg-emerald-400" },
          { label: "Launched",  dot: "bg-blue-400"    },
          { label: "Proposed",  dot: "bg-amber-400"   },
          { label: "Discussed", dot: "bg-slate-300"   },
          { label: "Continued", dot: "bg-purple-400"  },
        ].map(({ label, dot }) => (
          <span key={label} className="inline-flex items-center gap-1.5">
            <span className={`h-2.5 w-2.5 rounded-sm ${dot}`} aria-hidden />
            {label}
          </span>
        ))}
        <span className="ml-auto text-[11px] text-slate-400">
          Number badge = meetings that mention this initiative
        </span>
      </div>
    </div>
  );
}
