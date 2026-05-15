"use client";
import { useEffect } from "react";

/**
 * Root error boundary. Next.js renders this when a server component
 * throws or a downstream boundary catches an uncaught client error.
 * Must be a client component so the reset button can re-render the segment.
 */
export default function RootError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // TODO: wire this to your telemetry of choice once available.
    if (process.env.NODE_ENV !== "production") {
      console.error("Route error:", error);
    }
  }, [error]);

  return (
    <div className="mx-auto max-w-xl py-24 text-center">
      <div className="mb-6 flex justify-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-red-50 text-red-500">
          <svg className="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </div>
      </div>
      <h1 className="text-xl font-semibold text-slate-900">Something went wrong</h1>
      <p className="mt-2 text-sm text-slate-500">
        {error.message || "The page failed to load. This usually means the API is unreachable."}
      </p>
      {error.digest && (
        <p className="mt-1 font-mono text-[10px] text-slate-300">ref: {error.digest}</p>
      )}
      <button
        onClick={reset}
        className="mt-6 rounded-lg bg-indigo-600 px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
      >
        Try again
      </button>
    </div>
  );
}
