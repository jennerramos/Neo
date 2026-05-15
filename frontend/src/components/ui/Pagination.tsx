"use client";

interface PaginationProps {
  total: number;
  page: number;
  limit: number;
  onPageChange: (page: number) => void;
  /** Label for the items being paginated — shown as "X–Y of Z {label}". Defaults to "results". */
  label?: string;
}

/**
 * Simple prev/next pagination. Hides itself when there's only one page.
 */
export default function Pagination({
  total,
  page,
  limit,
  onPageChange,
  label = "results",
}: PaginationProps) {
  if (total <= limit) return null;

  const start = page * limit + 1;
  const end = Math.min((page + 1) * limit, total);
  const isLast = (page + 1) * limit >= total;

  return (
    <div className="flex items-center justify-between text-sm text-slate-500">
      <span>
        Showing {start.toLocaleString()}–{end.toLocaleString()} of {total.toLocaleString()} {label}
      </span>
      <div className="flex gap-2">
        <button
          disabled={page === 0}
          onClick={() => onPageChange(page - 1)}
          className="rounded border border-slate-200 px-3 py-1 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          ← Prev
        </button>
        <button
          disabled={isLast}
          onClick={() => onPageChange(page + 1)}
          className="rounded border border-slate-200 px-3 py-1 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
