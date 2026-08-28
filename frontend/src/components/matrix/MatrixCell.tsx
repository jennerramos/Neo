"use client";
import { useState } from "react";
import type { InsightCell } from "@/types";
import { actionTypeBorder, cn, fmtDate } from "@/lib/utils";

interface MatrixCellProps {
  cells: InsightCell[];
  /** How many to show before "+N more". The rest are already loaded. */
  previewLimit?: number;
  /** The currently selected insight id, if any (for highlighting). */
  activeInsightId?: string | null;
  /** Called when any individual insight in this cell is clicked. */
  onSelect?: (cell: InsightCell) => void;
  /** Called on hover to drive row/column highlighting. */
  onHover?: () => void;
}

/** A filled dot marks an insight backed by a reported figure; a hollow ring
 *  marks a recorded board action. Discussion gets no mark, so the eye lands on
 *  the items with something behind them. */
function EvidenceMark({ level }: { level: InsightCell["evidence_level"] }) {
  if (level === "measured") {
    return (
      <span
        className="mt-1 h-2 w-2 shrink-0 rounded-full bg-emerald-500"
        title="Measured result reported"
        aria-label="Measured result reported"
      />
    );
  }
  if (level === "action") {
    return (
      <span
        className="mt-1 h-2 w-2 shrink-0 rounded-full border-[1.5px] border-blue-500"
        title="Board action recorded"
        aria-label="Board action recorded"
      />
    );
  }
  return <span className="mt-1 h-2 w-2 shrink-0" aria-hidden />;
}

/**
 * Renders all insights for a single (theme × school) pair as a stacked
 * list of clickable labels. Each row gets a thick coloured left border
 * driven by action_type — trustees can sweep the matrix and spot which
 * items are approved vs discussed without reading every label.
 *
 * Every row also carries its meeting date. The matrix previously showed no
 * date anywhere, which let an item from a 2021 meeting read as current.
 *
 * A cell shows `previewLimit` items collapsed. The rest are already in the
 * payload — the API stopped truncating cells, because a cap there left half
 * the window's insights with no route to them at all.
 */
export default function MatrixCell({
  cells,
  previewLimit = 3,
  activeInsightId,
  onSelect,
  onHover,
}: MatrixCellProps) {
  const [expanded, setExpanded] = useState(false);

  if (cells.length === 0) {
    return (
      <div
        onMouseEnter={onHover}
        className="flex h-full min-h-[88px] items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50/60 px-2 text-center"
      >
        <span className="text-[10px] leading-tight text-slate-400">
          Nothing recorded
          <br />
          in this period
        </span>
      </div>
    );
  }

  const hiddenCount = Math.max(0, cells.length - previewLimit);
  const visible = expanded ? cells : cells.slice(0, previewLimit);

  return (
    <div
      onMouseEnter={onHover}
      className="flex h-full min-h-[88px] flex-col divide-y divide-slate-100 overflow-hidden rounded-lg border border-slate-200 bg-white"
    >
      {visible.map((cell) => {
        const active = cell.insight_id === activeInsightId;
        const spansMultiple =
          cell.meeting_count > 1 &&
          cell.first_meeting_date &&
          cell.last_meeting_date &&
          cell.first_meeting_date !== cell.last_meeting_date;

        return (
          <button
            key={cell.insight_id}
            onClick={() => onSelect?.(cell)}
            title={`${cell.label} — ${cell.action_type} · ${cell.evidence_label} · ${
              cell.meeting_count > 1
                ? `${cell.meeting_count} meetings to ${fmtDate(cell.last_meeting_date)}`
                : fmtDate(cell.last_meeting_date)
            }`}
            className={cn(
              "group relative flex w-full flex-col gap-1 border-l-[5px] px-2.5 py-2 text-left transition-colors",
              "focus:outline-none focus:ring-2 focus:ring-inset focus:ring-indigo-400",
              actionTypeBorder(cell.action_type),
              active ? "bg-indigo-50" : "hover:bg-slate-50"
            )}
          >
            <span className="flex w-full items-start gap-1.5">
              <EvidenceMark level={cell.evidence_level} />
              <span
                className={cn(
                  "line-clamp-2 flex-1 text-[11px] font-medium leading-snug",
                  active ? "text-indigo-800" : "text-slate-700 group-hover:text-indigo-700"
                )}
              >
                {cell.label}
              </span>
            </span>
            <span className="flex items-center gap-1.5 pl-[14px] text-[10px] text-slate-400">
              <span>{fmtDate(cell.last_meeting_date)}</span>
              {cell.meeting_count > 1 && (
                <span
                  className="rounded-full bg-slate-100 px-1.5 font-semibold text-slate-500"
                  title={
                    spansMultiple
                      ? `${cell.meeting_count} meetings, ${fmtDate(
                          cell.first_meeting_date
                        )} to ${fmtDate(cell.last_meeting_date)}`
                      : `${cell.meeting_count} meetings`
                  }
                >
                  {cell.meeting_count} meetings
                </span>
              )}
            </span>
          </button>
        );
      })}

      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="w-full bg-slate-50/80 px-2.5 py-1.5 text-[10px] font-semibold text-slate-500 transition-colors hover:bg-indigo-50 hover:text-indigo-700 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-indigo-400"
        >
          {expanded ? "Show fewer" : `+${hiddenCount} more`}
        </button>
      )}
    </div>
  );
}
