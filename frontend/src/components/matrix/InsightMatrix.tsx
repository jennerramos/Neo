"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { InsightMatrix as InsightMatrixType, InsightCell } from "@/types";
import { themeColor, cn } from "@/lib/utils";
import MatrixCell from "./MatrixCell";
import InsightDrawer from "./InsightDrawer";

interface InsightMatrixProps {
  data: InsightMatrixType;
}

export default function InsightMatrix({ data }: InsightMatrixProps) {
  const { school_names, themes } = data;

  // Reorder columns by content density (most-populated school first). This
  // puts data-rich institutions on the left so trustees don't land on empty
  // Mt. San Antonio cells first. Ties broken alphabetically by name to keep
  // ordering stable.
  const school_slugs = useMemo(() => {
    const totals = new Map<string, number>();
    for (const slug of data.school_slugs) {
      let n = 0;
      for (const row of themes) n += (row.cells[slug] ?? []).length;
      totals.set(slug, n);
    }
    return [...data.school_slugs].sort((a, b) => {
      const diff = (totals.get(b) ?? 0) - (totals.get(a) ?? 0);
      if (diff !== 0) return diff;
      return (school_names[a] ?? a).localeCompare(school_names[b] ?? b);
    });
  }, [data.school_slugs, themes, school_names]);

  // Flat list of every insight in row-major order (theme → school → insight).
  // Used to power ← / → keyboard navigation between the drawer contents.
  const flatInsights = useMemo(() => {
    const out: InsightCell[] = [];
    for (const row of themes) {
      for (const slug of school_slugs) {
        const cells = row.cells[slug] ?? [];
        for (const cell of cells) out.push(cell);
      }
    }
    return out;
  }, [themes, school_slugs]);

  const byId = useMemo(() => {
    const map = new Map<string, InsightCell>();
    flatInsights.forEach((c) => map.set(c.insight_id, c));
    return map;
  }, [flatInsights]);

  const [activeCell, setActiveCell] = useState<InsightCell | null>(null);
  const [hoverPos, setHoverPos] = useState<{ row: number; col: number } | null>(null);
  const searchParams = useSearchParams();

  // Open the drawer from ?cell=<id> on first mount and on back/forward nav.
  useEffect(() => {
    const id = searchParams.get("cell");
    if (!id) { setActiveCell(null); return; }
    const match = byId.get(id);
    if (match) setActiveCell(match);
  }, [searchParams, byId]);

  // Reflect selection in the URL without triggering a server re-render.
  const syncUrl = useCallback((id: string | null) => {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("cell", id);
    else url.searchParams.delete("cell");
    window.history.replaceState({}, "", url);
  }, []);

  const selectCell = useCallback((cell: InsightCell | null) => {
    setActiveCell(cell);
    syncUrl(cell?.insight_id ?? null);
  }, [syncUrl]);

  // Keyboard navigation while the drawer is open.
  // ←/→ step through flatInsights. Escape closes.
  useEffect(() => {
    if (!activeCell) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        selectCell(null);
        return;
      }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const idx = flatInsights.findIndex((c) => c.insight_id === activeCell.insight_id);
      if (idx === -1) return;
      const delta = e.key === "ArrowRight" ? 1 : -1;
      const next = flatInsights[(idx + delta + flatInsights.length) % flatInsights.length];
      selectCell(next);
      e.preventDefault();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [activeCell, flatInsights, selectCell]);

  const tableRef = useRef<HTMLTableElement>(null);

  return (
    <>
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table
          ref={tableRef}
          className="w-full border-collapse"
          style={{ minWidth: `${school_slugs.length * 180 + 200}px` }}
          onMouseLeave={() => setHoverPos(null)}
        >
          <thead>
            <tr>
              <th className="sticky left-0 z-10 bg-slate-50 border-b border-r border-slate-200 px-4 py-3 text-left">
                <span className="text-xs font-semibold uppercase tracking-widest text-slate-400">
                  Criteria
                </span>
              </th>
              {school_slugs.map((slug, colIdx) => {
                const colActive = hoverPos?.col === colIdx;
                return (
                  <th
                    key={slug}
                    className={cn(
                      "border-b border-r border-slate-200 px-3 py-3 text-center last:border-r-0 transition-colors",
                      colActive && "bg-indigo-50/60"
                    )}
                  >
                    <div className="flex flex-col items-center gap-1">
                      <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-indigo-100 text-xs font-bold text-indigo-700">
                        {school_names[slug]?.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase()}
                      </span>
                      <span className="text-xs font-medium text-slate-700 max-w-[120px] leading-tight text-center">
                        {school_names[slug]}
                      </span>
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {themes.map((theme, rowIdx) => {
              const rowActive = hoverPos?.row === rowIdx;
              return (
                <tr key={theme.theme_key} className={cn(rowActive && "bg-indigo-50/40")}>
                  {/* Theme label — sticky left */}
                  <td
                    className={cn(
                      "sticky left-0 z-10 border-b border-r border-slate-200 w-[200px] px-4 py-3 align-top transition-colors",
                      rowActive ? "bg-indigo-50/80" : "bg-white"
                    )}
                  >
                    <div className="flex items-start gap-2">
                      <span className={cn(
                        "inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-white text-[10px] font-bold",
                        themeColor(theme.theme_key)
                      )}>
                        {theme.theme_key}
                      </span>
                      <span className="text-xs font-medium text-slate-700 leading-snug">
                        {theme.theme_label}
                      </span>
                    </div>
                  </td>

                  {/* Cells */}
                  {school_slugs.map((slug, colIdx) => {
                    const cells = theme.cells[slug] ?? [];
                    const colActive = hoverPos?.col === colIdx;
                    return (
                      <td
                        key={slug}
                        className={cn(
                          "border-b border-r border-slate-200 p-2 align-top last:border-r-0 transition-colors",
                          colActive && !rowActive && "bg-indigo-50/20"
                        )}
                        style={{ minWidth: "180px" }}
                      >
                        <MatrixCell
                          cells={cells}
                          activeInsightId={activeCell?.insight_id ?? null}
                          onSelect={selectCell}
                          onHover={() => setHoverPos({ row: rowIdx, col: colIdx })}
                        />
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Drawer */}
      <InsightDrawer
        cell={activeCell}
        onClose={() => selectCell(null)}
      />
    </>
  );
}
