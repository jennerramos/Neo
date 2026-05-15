"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { fetchMeetings, fetchSchools } from "@/lib/api";
import type { Meeting, School } from "@/types";
import { cn, fmtDate, fmtDuration } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";
import EmptyState from "@/components/ui/EmptyState";
import ErrorState from "@/components/ui/ErrorState";
import FilterBar from "@/components/ui/FilterBar";
import Pagination from "@/components/ui/Pagination";

const LIMIT = 20;

export default function MeetingsPage() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [schools, setSchools] = useState<School[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  // Store the raw error so ErrorState can humanize it.
  const [error, setError] = useState<unknown>(null);

  const [school, setSchool] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchMeetings({
        school: school || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: LIMIT,
        offset: page * LIMIT,
      });
      setMeetings(res.meetings);
      setTotal(res.pagination.total);
    } catch (e: unknown) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [school, dateFrom, dateTo, page]);

  useEffect(() => { fetchSchools().then(setSchools).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);

  const clear = () => { setSchool(""); setDateFrom(""); setDateTo(""); setPage(0); };

  const bindFilter = <T extends string>(setter: (v: T) => void) => (v: T) => {
    setter(v);
    setPage(0);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">Meetings</h1>
        <p className="page-subtitle">{total.toLocaleString()} board meetings indexed</p>
      </div>

      <FilterBar
        schools={schools}
        school={school}
        onSchoolChange={bindFilter(setSchool)}
        dateFrom={dateFrom}
        onDateFromChange={bindFilter(setDateFrom)}
        dateTo={dateTo}
        onDateToChange={bindFilter(setDateTo)}
        onClear={clear}
      />

      <div className="card overflow-hidden">
        {error ? (
          <ErrorState error={error} onRetry={load} />
        ) : loading ? (
          <div className="flex justify-center py-16"><Spinner size={28} /></div>
        ) : meetings.length === 0 ? (
          <EmptyState title="No meetings found" description="Try adjusting your filters." />
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                {["Title", "School", "Date", "Duration", "Words", "Score"].map((h) => (
                  <th key={h} className="table-header">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {meetings.map((m) => (
                <tr key={m.meeting_id} className="transition-colors hover:bg-slate-50">
                  <td className="table-cell">
                    <Link
                      href={`/meetings/${m.meeting_id}`}
                      className="line-clamp-1 block max-w-sm font-medium text-indigo-600 hover:underline"
                    >
                      {m.title ?? `Meeting #${m.meeting_id}`}
                    </Link>
                  </td>
                  <td className="table-cell text-slate-500">{m.school_name}</td>
                  <td className="table-cell whitespace-nowrap text-slate-500">{fmtDate(m.published_date)}</td>
                  <td className="table-cell text-slate-500">{fmtDuration(m.duration_seconds)}</td>
                  <td className="table-cell text-slate-500">{m.word_count?.toLocaleString() ?? "—"}</td>
                  <td className="table-cell">
                    {m.quality_score != null ? (
                      <span className={cn(
                        "font-medium",
                        m.quality_score >= 0.8 ? "text-emerald-600" : "text-amber-600"
                      )}>
                        {Math.round(m.quality_score * 100)}%
                      </span>
                    ) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Pagination total={total} page={page} limit={LIMIT} onPageChange={setPage} label="meetings" />
    </div>
  );
}
