"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  exportVotesCsvUrl,
  fetchSchools,
  fetchVotes,
  fetchVotesSummary,
} from "@/lib/api";
import type { School, Vote, VotesStats } from "@/types";
import { cn, fmtDate } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";
import SourceLink from "@/components/ui/SourceLink";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState from "@/components/ui/ErrorState";
import FilterBar, { FilterSelect } from "@/components/ui/FilterBar";
import Pagination from "@/components/ui/Pagination";
import StatCard from "@/components/ui/StatCard";

const LIMIT = 50;

/** Render a 0–1 fraction as a percentage string; returns "—" on null. */
function fmtPct(x: number | null | undefined): string {
  if (x == null) return "—";
  return `${Math.round(x * 100)}%`;
}

export default function VotesPage() {
  const [votes, setVotes] = useState<Vote[]>([]);
  const [schools, setSchools] = useState<School[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  // Store the raw error so ErrorState can humanize it.
  const [error, setError] = useState<unknown>(null);

  const [summary, setSummary] = useState<VotesStats | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<unknown>(null);

  const [school, setSchool] = useState("");
  const [passed, setPassed] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchVotes({
        school: school || undefined,
        passed: passed !== "" ? passed === "true" : undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: LIMIT,
        offset: page * LIMIT,
      });
      setVotes(res.votes);
      setTotal(res.pagination.total);
    } catch (e: unknown) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [school, passed, dateFrom, dateTo, page]);

  // Summary is scoped by school + date range. Pass/fail filter is omitted
  // on purpose — pass rate is only meaningful over the full set.
  const loadSummary = useCallback(async () => {
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const s = await fetchVotesSummary({
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

  const topMover = summary?.top_movers?.[0];

  const clear = () => { setSchool(""); setPassed(""); setDateFrom(""); setDateTo(""); setPage(0); };

  // Wrap state setters so any filter change resets pagination.
  const bindFilter = <T extends string>(setter: (v: T) => void) => (v: T) => {
    setter(v);
    setPage(0);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="page-title">Votes</h1>
          <p className="page-subtitle">{total.toLocaleString()} recorded votes</p>
        </div>
        <a
          href={exportVotesCsvUrl(school || undefined, dateFrom || undefined, dateTo || undefined)}
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
            label="Pass rate"
            value={fmtPct(summary?.pass_rate ?? null)}
            hint={
              summary
                ? `${summary.passed.toLocaleString()} of ${(summary.passed + summary.failed).toLocaleString()} decided`
                : undefined
            }
            accent="emerald"
            loading={summaryLoading}
          />
          {/* The interesting number for trustees isn't the 96% pass rate —
           *  it's the contested motions. Click jumps straight to them. */}
          <StatCard
            label="Failed motions"
            value={summary ? summary.failed.toLocaleString() : "0"}
            hint={
              summary && summary.total > 0
                ? `${Math.round((summary.failed / summary.total) * 100)}% of motions`
                : undefined
            }
            accent="red"
            loading={summaryLoading}
            onClick={
              summary && summary.failed > 0
                ? () => bindFilter(setPassed)("false")
                : undefined
            }
            actionHint="Click to filter the list to failed motions"
          />
          <StatCard
            label="Unanimous"
            value={fmtPct(summary?.unanimous_rate ?? null)}
            hint={summary ? `${summary.unanimous.toLocaleString()} motions` : undefined}
            accent="blue"
            loading={summaryLoading}
          />
          <StatCard
            label="Top mover"
            value={topMover?.name ?? "—"}
            hint={topMover ? `${topMover.cnt.toLocaleString()} motions moved` : undefined}
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
          label="Result"
          value={passed}
          onChange={bindFilter(setPassed)}
          options={[
            { value: "", label: "All" },
            { value: "true", label: "Passed" },
            { value: "false", label: "Failed" },
          ]}
        />
      </FilterBar>

      <div className="card overflow-hidden">
        {error ? (
          <ErrorState error={error} onRetry={load} />
        ) : loading ? (
          <div className="flex justify-center py-16"><Spinner size={28} /></div>
        ) : votes.length === 0 ? (
          <EmptyState title="No votes found" />
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                {["Result", "Motion", "School", "Date", "Vote Count"].map((h) => (
                  <th key={h} className="table-header">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {votes.map((v) => (
                <tr key={v.vote_id} className="hover:bg-slate-50">
                  <td className="table-cell">
                    <span className={cn(
                      "rounded-full px-2 py-0.5 text-xs font-bold",
                      v.passed ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-700"
                    )}>
                      {v.passed ? "PASSED" : v.passed === false ? "FAILED" : "—"}
                    </span>
                  </td>
                  <td className="table-cell max-w-xs">
                    <p className="line-clamp-2 text-sm text-slate-800">{v.motion_text ?? "—"}</p>
                    <div className="mt-0.5 flex items-center gap-2">
                      {v.moved_by && <span className="text-xs text-slate-400">Moved by {v.moved_by}</span>}
                      <SourceLink meetingId={v.meeting_id} chunkIds={v.chunk_ids} />
                    </div>
                  </td>
                  <td className="table-cell whitespace-nowrap text-slate-500">
                    <Link href={`/meetings/${v.meeting_id}`} className="hover:text-indigo-600 hover:underline">
                      {v.school_name}
                    </Link>
                  </td>
                  <td className="table-cell whitespace-nowrap text-slate-400">{fmtDate(v.published_date)}</td>
                  <td className="table-cell text-xs text-slate-400">
                    {v.yes_count != null ? `${v.yes_count}–${v.no_count ?? 0}` : "—"}
                    {v.unanimous && " · Unanimous"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Pagination total={total} page={page} limit={LIMIT} onPageChange={setPage} label="votes" />
    </div>
  );
}
