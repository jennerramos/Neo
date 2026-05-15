import { fetchMeeting } from "@/lib/api";
import { cn, fmtCurrency, fmtDate, fmtDuration } from "@/lib/utils";
import Link from "next/link";
import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

export default async function MeetingDetailPage({ params }: { params: { id: string } }) {
  let data;
  try {
    data = await fetchMeeting(Number(params.id));
  } catch {
    notFound();
  }
  const { meeting: m, votes, financials, personnel, key_chunks } = data;

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* Breadcrumb */}
      <nav className="text-sm text-slate-400">
        <Link href="/meetings" className="hover:text-indigo-600">Meetings</Link>
        <span className="mx-2">/</span>
        <span className="text-slate-700 font-medium">{m.title ?? `Meeting #${m.meeting_id}`}</span>
      </nav>

      {/* Header */}
      <div className="card px-8 py-6">
        <h1 className="text-xl font-bold text-slate-900">{m.title}</h1>
        <div className="mt-2 flex flex-wrap gap-4 text-sm text-slate-500">
          <span>{m.school_name}</span>
          <span>·</span>
          <span>{fmtDate(m.published_date)}</span>
          {m.duration_seconds && <><span>·</span><span>{fmtDuration(m.duration_seconds)}</span></>}
          {m.word_count && <><span>·</span><span>{m.word_count.toLocaleString()} words</span></>}
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Link
            href={`/meetings/${m.meeting_id}/transcript`}
            className="rounded-lg border border-indigo-300 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 transition hover:bg-indigo-600 hover:text-white"
          >
            Read full transcript →
          </Link>
          {m.video_id && (
            <a
              href={`https://www.youtube.com/watch?v=${m.video_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-red-300 hover:text-red-600"
            >
              Watch on YouTube ↗
            </a>
          )}
        </div>
      </div>

      {/* Votes */}
      {votes.length > 0 && (
        <Section title={`Votes (${votes.length})`} link={{ href: `/votes?meeting_id=${m.meeting_id}`, label: "See all" }}>
          <div className="card divide-y divide-slate-100">
            {votes.map((v) => (
              <div key={v.vote_id} className="flex items-start gap-3 px-5 py-3">
                <span className={cn(
                  "mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold",
                  v.passed ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-700"
                )}>
                  {v.passed ? "PASSED" : "FAILED"}
                </span>
                <div className="min-w-0">
                  <p className="text-sm text-slate-800">{v.motion_text}</p>
                  {(v.yes_count != null || v.no_count != null) && (
                    <p className="mt-0.5 text-xs text-slate-400">
                      Yes: {v.yes_count ?? 0} · No: {v.no_count ?? 0}
                      {v.unanimous && " · Unanimous"}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Financials */}
      {financials.length > 0 && (
        <Section title={`Financials (${financials.length})`} link={{ href: `/financials?meeting_id=${m.meeting_id}`, label: "See all" }}>
          <div className="card divide-y divide-slate-100">
            {financials.map((f) => (
              <div key={f.item_id} className="flex items-center justify-between px-5 py-3">
                <div className="min-w-0">
                  <p className="text-sm text-slate-800 truncate">{f.description ?? f.vendor ?? f.category ?? "—"}</p>
                  <p className="text-xs text-slate-400">{f.action_type} · {f.category}</p>
                </div>
                <span className="ml-4 shrink-0 font-semibold text-slate-900">{fmtCurrency(f.amount)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Personnel */}
      {personnel.length > 0 && (
        <Section title={`Personnel Actions (${personnel.length})`}>
          <div className="card divide-y divide-slate-100">
            {personnel.map((p) => (
              <div key={p.action_id} className="flex items-center gap-3 px-5 py-3">
                <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                  {p.action_type}
                </span>
                <div>
                  <p className="text-sm font-medium text-slate-800">{p.person_name ?? "—"}</p>
                  <p className="text-xs text-slate-400">{p.position} {p.department ? `· ${p.department}` : ""}</p>
                </div>
                {p.is_interim && <span className="ml-auto text-xs text-amber-600 font-medium">Interim</span>}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Highlights (top-N by quality) — full transcript linked from the header */}
      {key_chunks.length > 0 && (
        <Section
          title={`Highlights (${key_chunks.length})`}
          link={{ href: `/meetings/${m.meeting_id}/transcript`, label: "Read full transcript →" }}
        >
          <div className="space-y-3">
            {key_chunks.map((c) => (
              <Link
                key={c.chunk_id}
                href={`/meetings/${m.meeting_id}/transcript#chunk-${c.chunk_id}`}
                className="card block px-5 py-4 transition-colors hover:border-indigo-300 hover:bg-indigo-50/30"
              >
                {c.speaker && (
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">{c.speaker}</p>
                )}
                <p className="text-sm text-slate-700 leading-relaxed">{c.text}</p>
                {c.quality_score != null && (
                  <p className="mt-2 text-[10px] text-slate-300">Quality: {Math.round(c.quality_score * 100)}%</p>
                )}
              </Link>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function Section({
  title, children, link,
}: {
  title: string;
  children: React.ReactNode;
  link?: { href: string; label: string };
}) {
  return (
    <section>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-widest text-slate-400">{title}</h2>
        {link && <Link href={link.href} className="text-xs text-indigo-600 hover:underline">{link.label}</Link>}
      </div>
      {children}
    </section>
  );
}
