import Link from "next/link";

/**
 * Deep link from an extracted row back to the transcript that produced it.
 *
 * Every vote / financial item carries `chunk_ids` — the chunks the extractor
 * quoted when it created the row (migration 0006). This lands on the same
 * `#chunk-<id>` anchor an /ask citation uses, so the transcript page scrolls
 * to and highlights the passage.
 *
 * Renders nothing for rows extracted before v2.6, which have no chunk_ids.
 * That is deliberate: a link that scrolls nowhere is worse than no link.
 */
export default function SourceLink({
  meetingId,
  chunkIds,
  className,
}: {
  meetingId: number;
  chunkIds?: string[] | null;
  className?: string;
}) {
  const chunkId = chunkIds?.[0];
  if (!chunkId) return null;

  return (
    <Link
      href={`/meetings/${meetingId}/transcript#chunk-${chunkId}`}
      className={
        className ??
        "inline-flex items-center gap-1 text-xs text-slate-400 hover:text-indigo-600 hover:underline"
      }
      title="Read the transcript passage this came from"
    >
      <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
        <path d="M2.5 3.5h7a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1Z" />
        <path d="M4 6h4M4 8.5h4M4 11h2.5" strokeLinecap="round" />
        <path d="M12 5.5v7a1 1 0 0 1-1 1H5" />
      </svg>
      Source
    </Link>
  );
}
