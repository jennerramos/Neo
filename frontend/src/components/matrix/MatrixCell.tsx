"use client";
import type { InsightCell } from "@/types";
import { actionTypeBorder, cn } from "@/lib/utils";

interface MatrixCellProps {
  cells: InsightCell[];
  /** The currently selected insight id, if any (for highlighting). */
  activeInsightId?: string | null;
  /** Called when any individual insight in this cell is clicked. */
  onSelect?: (cell: InsightCell) => void;
  /** Called on hover to drive row/column highlighting. */
  onHover?: () => void;
}

/**
 * Renders all insights for a single (theme × school) pair as a stacked
 * list of clickable labels. Each row gets a thick coloured left border
 * driven by action_type — trustees can sweep the matrix and spot which
 * items are approved vs discussed without reading every label.
 */
export default function MatrixCell({ cells, activeInsightId, onSelect, onHover }: MatrixCellProps) {
  if (cells.length === 0) {
    return (
      <div
        onMouseEnter={onHover}
        className="h-full min-h-[88px] rounded-lg border border-dashed border-slate-200 bg-slate-50/60"
        aria-label="No signal detected"
      />
    );
  }

  return (
    <div
      onMouseEnter={onHover}
      className="flex h-full min-h-[88px] flex-col divide-y divide-slate-100 overflow-hidden rounded-lg border border-slate-200 bg-white"
    >
      {cells.map((cell) => {
        const active = cell.insight_id === activeInsightId;
        return (
          <button
            key={cell.insight_id}
            onClick={() => onSelect?.(cell)}
            title={`${cell.label} — ${cell.action_type}${cell.meeting_count > 1 ? ` · ${cell.meeting_count} meetings` : ""}`}
            className={cn(
              "group relative flex w-full items-start gap-2 border-l-[5px] px-2.5 py-2 text-left transition-colors",
              "focus:outline-none focus:ring-2 focus:ring-inset focus:ring-indigo-400",
              actionTypeBorder(cell.action_type),
              active ? "bg-indigo-50" : "hover:bg-slate-50"
            )}
          >
            <span
              className={cn(
                "line-clamp-2 flex-1 text-[11px] font-medium leading-snug",
                active ? "text-indigo-800" : "text-slate-700 group-hover:text-indigo-700"
              )}
            >
              {cell.label}
            </span>
            {cell.meeting_count > 1 && (
              <span
                className="mt-0.5 shrink-0 rounded-full bg-slate-100 px-1.5 text-[10px] font-semibold text-slate-500"
                aria-label={`${cell.meeting_count} meetings mention this`}
                title={`${cell.meeting_count} meetings mention this initiative`}
              >
                {cell.meeting_count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
