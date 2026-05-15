import Link from "next/link";
import { fmtDate, themeColor } from "@/lib/utils";

export interface BriefingItem {
  id: string;
  /** Theme key like "T1" — shows as the color bar + label. */
  theme_key?: string;
  theme_label?: string;
  /** School badge label (full name). Initials are derived. */
  school_name: string;
  school_slug: string;
  /** One-line headline, e.g., "HCC approves AI & Robotics AAS program". */
  headline: string;
  /** Two to three short sentences explaining what happened. */
  summary?: string;
  /** Date of the underlying meeting / action. */
  date?: string | null;
  /** Where the card links to — typically an insight or meeting. */
  href: string;
}

export default function BriefingCard({ item }: { item: BriefingItem }) {
  const initials = item.school_name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <Link
      href={item.href}
      className="group flex min-w-[260px] flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm transition hover:-translate-y-0.5 hover:border-indigo-300 hover:shadow-md sm:min-w-0"
    >
      {/* Header: school badge + theme tag */}
      <div className="flex items-center gap-2">
        <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-indigo-100 text-[10px] font-bold text-indigo-700">
          {initials}
        </span>
        <span className="truncate text-xs font-medium text-slate-500">{item.school_name}</span>
        {item.theme_key && (
          <span
            className={`ml-auto inline-flex h-5 items-center gap-1 rounded px-1.5 text-[10px] font-bold uppercase tracking-wide text-white ${themeColor(item.theme_key)}`}
            title={item.theme_label}
          >
            {item.theme_key}
          </span>
        )}
      </div>

      {/* Headline */}
      <p className="mt-3 text-sm font-semibold leading-snug text-slate-900 group-hover:text-indigo-700">
        {item.headline}
      </p>

      {/* Summary */}
      {item.summary && (
        <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-slate-500">
          {item.summary}
        </p>
      )}

      {/* Footer */}
      <div className="mt-3 flex items-center justify-between text-[11px] text-slate-400">
        <span>{fmtDate(item.date)}</span>
        <span className="opacity-0 transition-opacity group-hover:opacity-100 text-indigo-500">→</span>
      </div>
    </Link>
  );
}
