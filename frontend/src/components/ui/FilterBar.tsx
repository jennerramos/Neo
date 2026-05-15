"use client";
import type { School } from "@/types";

interface FilterBarProps {
  /** Available schools. If omitted, the school filter is hidden. */
  schools?: School[];
  school?: string;
  onSchoolChange?: (value: string) => void;

  /** ISO date strings (yyyy-mm-dd). If either handler is omitted, the date range is hidden. */
  dateFrom?: string;
  dateTo?: string;
  onDateFromChange?: (value: string) => void;
  onDateToChange?: (value: string) => void;

  /** Called when the user clicks "Clear". If omitted, no clear button is shown. */
  onClear?: () => void;

  /** Slot for page-specific fields (e.g., result filter on /votes). Rendered before the clear button. */
  children?: React.ReactNode;
}

/**
 * Shared filter bar for list pages (Votes, Financials, Meetings).
 * Pages compose their unique filters via children; common ones
 * (school + date range) are built in.
 */
export default function FilterBar({
  schools,
  school = "",
  onSchoolChange,
  dateFrom = "",
  dateTo = "",
  onDateFromChange,
  onDateToChange,
  onClear,
  children,
}: FilterBarProps) {
  return (
    <div className="card flex flex-wrap items-end gap-3 p-4">
      {schools && onSchoolChange && (
        <FilterField label="School">
          <select
            value={school}
            onChange={(e) => onSchoolChange(e.target.value)}
            className="form-select"
          >
            <option value="">All schools</option>
            {schools.map((s) => (
              <option key={s.slug} value={s.slug}>{s.name}</option>
            ))}
          </select>
        </FilterField>
      )}

      {children}

      {onDateFromChange && (
        <FilterField label="From">
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => onDateFromChange(e.target.value)}
            className="form-input"
          />
        </FilterField>
      )}

      {onDateToChange && (
        <FilterField label="To">
          <input
            type="date"
            value={dateTo}
            onChange={(e) => onDateToChange(e.target.value)}
            className="form-input"
          />
        </FilterField>
      )}

      {onClear && (
        <button
          onClick={onClear}
          className="self-end pb-1 text-sm text-slate-400 hover:text-slate-600"
        >
          Clear
        </button>
      )}
    </div>
  );
}

export function FilterField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-slate-500">{label}</span>
      {children}
    </label>
  );
}

/** Pre-styled select for use inside FilterBar. */
export function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <FilterField label={label}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="form-select"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </FilterField>
  );
}
