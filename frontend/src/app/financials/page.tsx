"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  exportFinancialsCsvUrl,
  fetchFinancials,
  fetchFinancialsSummary,
  fetchSchools,
} from "@/lib/api";
import type { Financial, FinancialsStats, School } from "@/types";
import { cn, fmtCurrency, fmtDate } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState from "@/components/ui/ErrorState";
import FilterBar, { FilterSelect } from "@/components/ui/FilterBar";
import Pagination from "@/components/ui/Pagination";
import StatCard from "@/components/ui/StatCard";

const LIMIT = 50;

export default function FinancialsPage() {
  const router = useRouter();
  const [items, setItems] = useState<Financial[]>([]);
  const [schools, setSchools] = useState<School[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  // Store the raw error so ErrorState can humanize it and show the detail.
  const [error, setError] = useState<unknown>(null);

  const [summary, setSummary] = useState<FinancialsStats | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<unknown>(null);

  const [school, setSchool] = useState("");
  const [actionType, setActionType] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchFinancials({
        school: school || undefined,
        action_type: actionType || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: LIMIT,
        offset: page * LIMIT,
      });
      setItems(res.items);
      setTotal(res.pagination.total);
    } catch (e: unknown) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [school, actionType, dateFrom, dateTo, page]);

  // Summary is scoped by school + date range only (action_type would
  // defeat the purpose of showing an approved/proposed breakdown).
  const loadSummary = useCallback(async () => {
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const s = await fetchFinancialsSummary({
        school: school || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
      });
      setSummary(s);
    } catch (e: unknown) {
      setSummary(null);
      setSummaryError(e);
    } finally {
      setSummaryLoading(false);
    }
  }, [school, dateFrom, dateTo]);

  useEffect(() => { fetchSchools().then(setSchools).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadSummary(); }, [loadSummary]);

  // Derive card values from the summary payload.
  const headline = useMemo(() => {
    if (!summary) return null;
    const byType = Object.fromEntries(
      summary.by_action_type.map((r) => [r.action_type ?? "unknown", r])
    );
    const approved = byType["approved"];
    const proposed = byType["proposed"];
    const itemsTracked = summary.by_action_type.reduce((a, r) => a + (r.cnt ?? 0), 0);
    const topVendor = summary.top_vendors[0];
    return {
      approvedTotal: approved?.total ?? 0,
      approvedCount: approved?.cnt ?? 0,
      proposedTotal: proposed?.total ?? 0,
      proposedCount: proposed?.cnt ?? 0,
      itemsTracked,
      topVendor,
    };
  }, [summary]);

  const clear = () => { setSchool(""); setActionType(""); setDateFrom(""); setDateTo(""); setPage(0); };

  const bindFilter = <T extends string>(setter: (v: T) => void) => (v: T) => {
    setter(v);
    setPage(0);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="page-title">Financials</h1>
          <p className="page-subtitle">{total.toLocaleString()} financial items</p>
        </div>
        <a
          href={exportFinancialsCsvUrl(school || undefined, dateFrom || undefined, dateTo || undefined)}
          className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-50"
          download
        >
          ↓ Export CSV
        </a>
      </div>

      {summaryError ? (
        <ErrorState
          error={summaryError}
          size="inline"
          title="We couldn't load the summary cards."
          onRetry={loadSummary}
        />
      ) : (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard
            label="Approved"
            value={fmtCurrency(headline?.approvedTotal ?? 0)}
            hint={headline ? `${headline.approvedCount.toLocaleString()} items` : undefined}
            accent="emerald"
            loading={summaryLoading}
          />
          <StatCard
            label="Proposed"
            value={fmtCurrency(headline?.proposedTotal ?? 0)}
            hint={headline ? `${headline.proposedCount.toLocaleString()} items` : undefined}
            accent="amber"
            loading={summaryLoading}
          />
          {/* The single largest line item — far more actionable for a
           *  trustee than "items tracked: 127". Click navigates straight
           *  to the originating meeting. */}
          <StatCard
            label="Largest item"
            value={summary?.largest_item ? fmtCurrency(summary.largest_item.amount) : "—"}
            hint={
              summary?.largest_item
                ? (summary.largest_item.vendor
                    || summary.largest_item.description?.slice(0, 40)
                    || "see meeting")
                : undefined
            }
            accent="indigo"
            loading={summaryLoading}
            onClick={
              summary?.largest_item
                ? () => router.push(`/meetings/${summary.largest_item!.meeting_id}`)
                : undefined
            }
            actionHint="Click to open the meeting where this was approved"
          />
          <StatCard
            label="Top vendor"
            value={headline?.topVendor?.vendor ?? "—"}
            hint={headline?.topVendor ? fmtCurrency(headline.topVendor.total ?? 0) : undefined}
            accent="purple"
            loading={summaryLoading}
          />
        </div>
      )}

      <FilterBar
        schools={schools}
        school={school}
        onSchoolChange={bindFilter(setSchool)}
        dateFrom={dateFrom}
        onDateFromChange={bindFilter(setDateFrom)}
        dateTo={dateTo}
        onDateToChange={bindFilter(setDateTo)}
        onClear={clear}
      >
        <FilterSelect
          label="Type"
          value={actionType}
          onChange={bindFilter(setActionType)}
          options={[
            { value: "", label: "All types" },
            { value: "approved", label: "Approved" },
            { value: "proposed", label: "Proposed" },
            { value: "discussed", label: "Discussed" },
          ]}
        />
      </FilterBar>

      <div className="card overflow-hidden">
        {error ? (
          <ErrorState error={error} onRetry={load} />
        ) : loading ? (
          <div className="flex justify-center py-16"><Spinner size={28} /></div>
        ) : items.length === 0 ? (
          <EmptyState title="No financial items found" />
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                {["Type", "Description / Vendor", "Category", "Amount", "School", "Date"].map((h) => (
                  <th key={h} className="table-header">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((f) => (
                <tr key={f.item_id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <span className={cn(
                      "rounded-full px-2 py-0.5 text-xs font-semibold uppercase",
                      f.action_type === "approved" ? "bg-emerald-100 text-emerald-800" :
                      f.action_type === "proposed" ? "bg-amber-100 text-amber-800" :
                      "bg-slate-100 text-slate-600"
                    )}>
                      {f.action_type ?? "—"}
                    </span>
                  </td>
                  <td className="table-cell max-w-xs">
                    <p className="line-clamp-2 text-sm text-slate-800">{f.description ?? "—"}</p>
                    {f.vendor && <p className="mt-0.5 text-xs text-slate-400">{f.vendor}</p>}
                  </td>
                  <td className="table-cell text-xs text-slate-500">{f.category ?? "—"}</td>
                  <td className="table-cell font-semibold text-slate-900">{fmtCurrency(f.amount)}</td>
                  <td className="table-cell text-slate-500">
                    <Link href={`/meetings/${f.meeting_id}`} className="hover:text-indigo-600 hover:underline">
                      {f.school_name}
                    </Link>
                  </td>
                  <td className="table-cell whitespace-nowrap text-slate-400">{fmtDate(f.published_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Pagination total={total} page={page} limit={LIMIT} onPageChange={setPage} label="items" />
    </div>
  );
}
