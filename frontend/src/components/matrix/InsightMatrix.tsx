"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { InsightMatrix as InsightMatrixType, InsightCell, ThemeRow } from "@/types";
import { themeColor, cn } from "@/lib/utils";
import FilterBar, { FilterField, FilterSelect } from "@/components/ui/FilterBar";
import MatrixCell from "./MatrixCell";
import InsightDrawer from "./InsightDrawer";

interface InsightMatrixProps {
  data: InsightMatrixType;
}

/** The seven action states the extractor emits, in board-process order. */
const ACTION_OPTIONS = [
  { value: "", label: "Any action" },
  { value: "approved", label: "Approved" },
  { value: "launched", label: "Launched" },
  { value: "proposed", label: "Proposed" },
  { value: "discussed", label: "Discussed" },
  { value: "continued", label: "Continued" },
  { value: "cancelled", label: "Cancelled" },
  { value: "other", label: "Other" },
];

const EVIDENCE_OPTIONS = [
  { value: "", label: "Any evidence" },
  { value: "measured", label: "Measured result reported" },
  { value: "action", label: "Board action recorded" },
  { value: "discussion", label: "Mentioned in discussion" },
];

const EMPTY_FILTERS = {
  q: "",
  school: "",
  theme: "",
  action: "",
  evidence: "",
  from: "",
  to: "",
};

export default function InsightMatrix({ data }: InsightMatrixProps) {
  const { school_names, themes: allThemes, preview_limit } = data;

  // Filters. Every insight in the window is in `data` — the API no longer
  // truncates cells — so this narrows what is on screen without a round trip.
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const set = useCallback(
    (key: keyof typeof EMPTY_FILTERS) => (value: string) =>
      setFilters((f) => ({ ...f, [key]: value })),
    []
  );
  const filtersActive = useMemo(
    () => Object.values(filters).some((v) => v !== ""),
    [filters]
  );

  const matches = useCallback(
    (c: InsightCell) => {
      if (filters.school && c.school_slug !== filters.school) return false;
      if (filters.theme && c.theme_key !== filters.theme) return false;
      if (filters.action && c.action_type?.toLowerCase() !== filters.action) return false;
      if (filters.evidence && c.evidence_level !== filters.evidence) return false;
      // Date bounds compare against the span the insight covers, so an item
      // running from March to July is found by a search for either month.
      if (filters.from && (c.last_meeting_date ?? "") < filters.from) return false;
      if (filters.to && (c.first_meeting_date ?? "9999") > filters.to) return false;
      if (filters.q) {
        const needle = filters.q.toLowerCase();
        const hay = `${c.label} ${c.school_name} ${c.theme_label} ${c.action_type}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    },
    [filters]
  );

  const themes: ThemeRow[] = useMemo(() => {
    if (!filtersActive) return allThemes;
    return allThemes.map((row) => ({
      ...row,
      cells: Object.fromEntries(
        Object.entries(row.cells).map(([slug, cells]) => [slug, cells.filter(matches)])
      ),
    }));
  }, [allThemes, filtersActive, matches]);

  const matchCount = useMemo(
    () =>
      themes.reduce(
        (n, row) => n + Object.values(row.cells).reduce((m, cells) => m + cells.length, 0),
        0
      ),
    [themes]
  );

  // Reorder columns by content density (most-populated school first). This
  // puts data-rich institutions on the left so trustees don't land on empty
  // Mt. San Antonio cells first. Ties broken alphabetically by name to keep
  // ordering stable.
  // Ordered on the UNFILTERED totals, so columns keep their places while the
  // reader types rather than resorting under the cursor.
  const orderedSlugs = useMemo(() => {
    const totals = new Map<string, number>();
    for (const slug of data.school_slugs) {
      let n = 0;
      for (const row of allThemes) n += (row.cells[slug] ?? []).length;
      totals.set(slug, n);
    }
    return [...data.school_slugs].sort((a, b) => {
      const diff = (totals.get(b) ?? 0) - (totals.get(a) ?? 0);
      if (diff !== 0) return diff;
      return (school_names[a] ?? a).localeCompare(school_names[b] ?? b);
    });
  }, [data.school_slugs, allThemes, school_names]);

  // While filtering, drop columns and rows with nothing left in them — a
  // search for one college should not leave seven empty columns on screen.
  // Unfiltered, every cell stays: an empty cell reads "Nothing recorded in
  // this period", which is itself a fact about the corpus.
  const school_slugs = useMemo(() => {
    if (!filtersActive) return orderedSlugs;
    return orderedSlugs.filter((slug) =>
      themes.some((row) => (row.cells[slug] ?? []).length > 0)
    );
  }, [orderedSlugs, themes, filtersActive]);

  const visibleThemes = useMemo(() => {
    if (!filtersActive) return themes;
    return themes.filter((row) =>
      Object.values(row.cells).some((cells) => cells.length > 0)
    );
  }, [themes, filtersActive]);

  // Flat list of every insight in row-major order (theme → school → insight).
  // Used to power ← / → keyboard navigation between the drawer contents.
  const flatInsights = useMemo(() => {
    const out: InsightCell[] = [];
    for (const row of visibleThemes) {
      for (const slug of school_slugs) {
        const cells = row.cells[slug] ?? [];
        for (const cell of cells) out.push(cell);
      }
    }
    return out;
  }, [visibleThemes, school_slugs]);

  // Every insight, filtered or not — a ?cell= link has to resolve even when
  // the current filters would hide its cell.
  const byId = useMemo(() => {
    const map = new Map<string, InsightCell>();
    for (const row of allThemes) {
      for (const cells of Object.values(row.cells)) {
        for (const cell of cells) map.set(cell.insight_id, cell);
      }
    }
    return map;
  }, [allThemes]);

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
      <FilterBar
        dateFrom={filters.from}
        onDateFromChange={set("from")}
        dateTo={filters.to}
        onDateToChange={set("to")}
        onClear={filtersActive ? () => setFilters(EMPTY_FILTERS) : undefined}
      >
        <FilterField label="Search">
          <input
            type="search"
            value={filters.q}
            onChange={(e) => set("q")(e.target.value)}
            placeholder="Initiative, college, theme…"
            className="form-input min-w-[220px]"
          />
        </FilterField>
        <FilterSelect
          label="College"
          value={filters.school}
          onChange={set("school")}
          options={[
            { value: "", label: "All colleges" },
            ...orderedSlugs.map((slug) => ({
              value: slug,
              label: school_names[slug] ?? slug,
            })),
          ]}
        />
        <FilterSelect
          label="Theme"
          value={filters.theme}
          onChange={set("theme")}
          options={[
            { value: "", label: "All themes" },
            ...allThemes.map((t) => ({ value: t.theme_key, label: t.theme_label })),
          ]}
        />
        <FilterSelect
          label="Action"
          value={filters.action}
          onChange={set("action")}
          options={ACTION_OPTIONS}
        />
        <FilterSelect
          label="Evidence"
          value={filters.evidence}
          onChange={set("evidence")}
          options={EVIDENCE_OPTIONS}
        />
      </FilterBar>

      <p className="-mt-2 mb-4 text-xs text-slate-500" aria-live="polite">
        {filtersActive ? (
          <>
            <span className="font-semibold text-slate-700">{matchCount}</span> of{" "}
            {data.insight_count} items match.{" "}
            <button
              onClick={() => setFilters(EMPTY_FILTERS)}
              className="text-indigo-600 underline-offset-2 hover:underline"
            >
              Show all
            </button>
          </>
        ) : (
          <>
            All <span className="font-semibold text-slate-700">{data.insight_count}</span>{" "}
            items recorded in this period are on this page. Cells show the{" "}
            {preview_limit} strongest; open a cell for the rest.
          </>
        )}
      </p>

      {filtersActive && matchCount === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50/60 px-6 py-14 text-center">
          <p className="text-sm font-medium text-slate-700">No items match these filters.</p>
          <p className="mt-1 text-sm text-slate-500">
            Try a broader search, or{" "}
            <button
              onClick={() => setFilters(EMPTY_FILTERS)}
              className="text-indigo-600 underline-offset-2 hover:underline"
            >
              clear the filters
            </button>
            . Older business is outside this page&rsquo;s {data.window_months}-month period —
            ask Neo about it directly.
          </p>
        </div>
      ) : (
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
            {visibleThemes.map((theme, rowIdx) => {
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
                          previewLimit={preview_limit}
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
      )}

      {/* Drawer */}
      <InsightDrawer
        cell={activeCell}
        onClose={() => selectCell(null)}
      />
    </>
  );
}
