import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-xl py-24 text-center">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-600">404</p>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900">
        This page doesn&rsquo;t exist
      </h1>
      <p className="mt-2 text-sm text-slate-500">
        The insight, meeting, or record you&rsquo;re looking for may have been removed or never indexed.
      </p>
      <Link
        href="/"
        className="mt-6 inline-block rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
      >
        ← Ask Neo anything
      </Link>
    </div>
  );
}
