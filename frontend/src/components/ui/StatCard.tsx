import { cn } from "@/lib/utils";

export interface StatCardProps {
  /** Short label, upper-case presentation. */
  label: string;
  /** Big headline value — either a pre-formatted string or a number. */
  value: string | number;
  /** Optional secondary line shown under the value (e.g. "of 346 motions"). */
  hint?: string;
  /** Accent colour token for the left stripe — lets trustees associate cards
   *  with their underlying action type (green=approved, etc). */
  accent?: "emerald" | "blue" | "amber" | "purple" | "slate" | "indigo" | "red";
  /** Icon or trend indicator rendered top-right. Keep it small (w-4 h-4). */
  icon?: React.ReactNode;
  /** Render state when the parent is fetching. */
  loading?: boolean;
  /**
   * When provided, the card becomes interactive (button semantics, hover
   * state, focus ring, pointer cursor). Use for "click to filter" or "click
   * to navigate" affordances — the calling page decides what the click does.
   */
  onClick?: () => void;
  /** Tooltip + aria-label shown when onClick is set ("Click to filter to…"). */
  actionHint?: string;
}

const ACCENT_CLASSES: Record<NonNullable<StatCardProps["accent"]>, string> = {
  emerald: "before:bg-emerald-400",
  blue:    "before:bg-blue-400",
  amber:   "before:bg-amber-400",
  purple:  "before:bg-purple-400",
  slate:   "before:bg-slate-300",
  indigo:  "before:bg-indigo-400",
  red:     "before:bg-red-400",
};

/**
 * Compact summary card used at the top of data pages. Designed to work in a
 * 4-up grid on desktop and collapse to 2-up on tablet. The left stripe is a
 * pseudo-element so borders don't interfere with card rounding.
 *
 * Pass `onClick` to make the card a real button — adds hover lift, focus
 * ring, and a tiny chevron in the corner so users can tell it's actionable.
 */
export default function StatCard({
  label, value, hint, accent = "slate", icon, loading, onClick, actionHint,
}: StatCardProps) {
  const baseClasses = cn(
    "relative overflow-hidden rounded-xl border border-slate-200 bg-white px-5 py-4 shadow-sm",
    "before:absolute before:inset-y-0 before:left-0 before:w-1",
    ACCENT_CLASSES[accent],
  );

  const interactiveClasses = onClick && cn(
    "text-left transition-all duration-150",
    "hover:-translate-y-0.5 hover:shadow-md hover:border-slate-300",
    "focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-1",
    "cursor-pointer",
  );

  const inner = (
    <>
      <div className="flex items-start justify-between gap-2">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">
          {label}
        </p>
        {/* When the card is clickable, show a subtle chevron in the corner so
         *  the affordance is visible at a glance. Otherwise show whatever icon
         *  the caller passed (or nothing). */}
        {onClick ? (
          <span className="text-slate-300" aria-hidden="true">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M9 5l7 7-7 7" />
            </svg>
          </span>
        ) : icon ? (
          <span className="text-slate-300">{icon}</span>
        ) : null}
      </div>
      <p className={cn(
        "mt-1 text-2xl font-bold tabular-nums tracking-tight text-slate-900",
        loading && "animate-pulse text-slate-300"
      )}>
        {loading ? "—" : value}
      </p>
      {hint && (
        <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
      )}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        title={actionHint}
        aria-label={actionHint ?? `${label}: ${value}`}
        className={cn(baseClasses, interactiveClasses, "block w-full")}
      >
        {inner}
      </button>
    );
  }

  return <div className={baseClasses}>{inner}</div>;
}
