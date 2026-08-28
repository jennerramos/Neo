import Link from "next/link";

/**
 * Not-found for /insights/*.
 *
 * The generic 404 ("may have been removed or never indexed") is misleading
 * here: the usual reason an insight id no longer resolves is that the page
 * covers a rolling six-month window and the item has aged out of it. The
 * record still exists — it is simply no longer current board business, which
 * is a different thing to tell a trustee.
 */
export default function InsightNotFound() {
  return (
    <div className="mx-auto max-w-xl py-24 text-center">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-600">
        Not in this period
      </p>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900">
        We can&rsquo;t show this insight
      </h1>
      <p className="mt-3 text-sm leading-relaxed text-slate-500">
        Insights cover the last six months of board meetings. This one has either aged out of
        that period, or the link is no longer valid. Older business is still searchable — ask
        Neo about it directly and it will cite the meeting it came from.
      </p>
      <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
        <Link
          href="/insights"
          className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
        >
          Back to Insights
        </Link>
        <Link
          href="/"
          className="rounded-lg border border-slate-200 px-5 py-2 text-sm font-semibold text-slate-700 transition-colors hover:border-indigo-300 hover:text-indigo-700"
        >
          Ask Neo instead
        </Link>
      </div>
    </div>
  );
}
