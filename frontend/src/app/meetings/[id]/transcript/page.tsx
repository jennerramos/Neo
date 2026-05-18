"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { fetchMeetingTranscript } from "@/lib/api";
import type { MeetingTranscript } from "@/types";
import { cn, fmtDate, fmtDuration } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";

function fmtTs(sec: number | null | undefined): string {
  if (sec == null) return "—";
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
    : `${m}:${String(ss).padStart(2, "0")}`;
}

function youtubeUrl(videoId: string, startSec?: number | null): string {
  const base = `https://www.youtube.com/watch?v=${videoId}`;
  return startSec ? `${base}&t=${Math.floor(startSec)}s` : base;
}

function videoLabel(sourceType: string | null): string {
  if (sourceType === "panopto") return "Watch on Panopto ↗";
  if (sourceType === "ravnur") return "Watch on MediaPortal ↗";
  return "Watch on YouTube ↗";
}

function seekUrl(
  videoUrl: string | null,
  videoId: string,
  sourceType: string | null,
  startSec: number | null | undefined,
): string | null {
  if (startSec == null) return null;
  const sec = Math.floor(startSec);
  if (sourceType === "panopto" && videoUrl) {
    const sep = videoUrl.includes("?") ? "&" : "?";
    return `${videoUrl}${sep}start=${sec}`;
  }
  if (sourceType === "youtube_caption" || !sourceType) {
    return `https://www.youtube.com/watch?v=${videoId}&t=${sec}s`;
  }
  return null;
}

function seekTooltip(sourceType: string | null): string {
  if (sourceType === "panopto") return "Jump to this moment on Panopto";
  if (sourceType === "ravnur") return "MediaPortal doesn't support timestamp links";
  return "Jump to this moment on YouTube";
}

export default function TranscriptPage() {
  const params = useParams<{ id: string }>();
  const meetingId = Number(params.id);

  const [data, setData]   = useState<MeetingTranscript | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchMeetingTranscript(meetingId)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [meetingId]);

  // Track hash for the target-highlight effect and scroll on first paint after load.
  useEffect(() => {
    if (!data) return;
    const applyHash = () => {
      const h = window.location.hash.replace(/^#/, "");
      setAnchor(h || null);
      if (h) {
        const el = document.getElementById(h);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    };
    applyHash();
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, [data]);

  if (error) {
    return (
      <div className="card flex items-center justify-center py-20 text-sm text-red-600">{error}</div>
    );
  }

  if (!data) {
    return (
      <div className="flex justify-center py-20"><Spinner size={28} /></div>
    );
  }

  const { meeting: m, segments } = data;
  const videoHref = m.video_url ?? (m.video_id ? youtubeUrl(m.video_id) : null);

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {/* Breadcrumb */}
      <nav className="text-sm text-slate-400">
        <Link href="/meetings" className="hover:text-indigo-600">Meetings</Link>
        <span className="mx-2">/</span>
        <Link href={`/meetings/${m.meeting_id}`} className="hover:text-indigo-600">
          {m.title ?? `Meeting #${m.meeting_id}`}
        </Link>
        <span className="mx-2">/</span>
        <span className="text-slate-700 font-medium">Transcript</span>
      </nav>

      {/* Header */}
      <div className="card px-8 py-6">
        <h1 className="text-xl font-bold text-slate-900">{m.title ?? `Meeting #${m.meeting_id}`}</h1>
        <div className="mt-2 flex flex-wrap gap-3 text-sm text-slate-500">
          <span>{m.school_name}</span>
          <span>·</span>
          <span>{fmtDate(m.published_date)}</span>
          {m.duration_seconds && <><span>·</span><span>{fmtDuration(m.duration_seconds)}</span></>}
          <span>·</span>
          <span>{segments.length} segments</span>
          {m.word_count && <><span>·</span><span>{m.word_count.toLocaleString()} words</span></>}
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Link
            href={`/meetings/${m.meeting_id}`}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-indigo-300 hover:text-indigo-700"
          >
            ← Back to overview
          </Link>
          {videoHref && (
            <a
              href={videoHref}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-red-300 hover:text-red-600"
            >
              {videoLabel(m.source_type)}
            </a>
          )}
        </div>
      </div>

      {/* Segments */}
      {segments.length === 0 ? (
        <div className="card flex items-center justify-center py-16 text-sm text-slate-400">
          Transcript isn&apos;t available for this meeting yet.
        </div>
      ) : (
        <div className="space-y-3">
          {segments.map((s) => {
            const anchorId = `chunk-${s.chunk_id}`;
            const isActive = anchor === anchorId;
            return (
              <article
                key={s.chunk_id}
                id={anchorId}
                className={cn(
                  "card px-5 py-4 scroll-mt-20 transition-colors",
                  isActive && "border-indigo-400 bg-indigo-50/50 ring-2 ring-indigo-200"
                )}
              >
                <header className="mb-1.5 flex items-center gap-2 text-xs text-slate-400">
                  {s.speaker && (
                    <span className="font-semibold uppercase tracking-wide text-slate-500">
                      {s.speaker}
                    </span>
                  )}
                  {(() => {
                    const href = seekUrl(m.video_url, m.video_id, m.source_type, s.start_time);
                    if (href) {
                      return (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={seekTooltip(m.source_type)}
                          className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 hover:bg-red-100 hover:text-red-700"
                        >
                          {fmtTs(s.start_time)}
                        </a>
                      );
                    }
                    return (
                      <span
                        title={seekTooltip(m.source_type)}
                        className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                      >
                        {fmtTs(s.start_time)}
                      </span>
                    );
                  })()}
                  <a
                    href={`#${anchorId}`}
                    className="ml-auto text-slate-300 hover:text-indigo-500"
                    title="Link to this segment"
                  >
                    §
                  </a>
                </header>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
                  {s.text}
                </p>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
