"use client";
import { useState } from "react";
import { cn, humanizeApiError } from "@/lib/utils";

export interface ErrorStateProps {
  /** The thrown error — we'll humanize it. Accepts anything caught in a try/catch. */
  error: unknown;
  /** Optional retry callback. When provided, a Retry button is rendered. */
  onRetry?: () => void;
  /** How much vertical space to claim. "inline" for card rows, "block" for full-width table areas. */
  size?: "inline" | "block";
  /** Override the humanized message — useful when the caller already knows the context. */
  title?: string;
}

/**
 * Friendly error panel shown in place of failed content. Renders the raw
 * technical error behind a collapsible "Show details" toggle so trustees
 * see plain English first but devs/support can still dig in.
 *
 * Designed to scope to one section (summary-row vs table) so a single
 * fetch failure doesn't erase the whole page.
 */
export default function ErrorState({ error, onRetry, size = "block", title }: ErrorStateProps) {
  const [showDetails, setShowDetails] = useState(false);
  const { message, detail } = humanizeApiError(error);
  const displayMessage = title ?? message;

  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-xl border border-red-100 bg-red-50/50 text-center",
        size === "block" ? "py-16 px-6" : "py-6 px-4",
      )}
    >
      <div className="flex items-center gap-2 text-red-600">
        <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M12 9v3.75m0 3v.008M3.75 17.25h16.5a1.5 1.5 0 001.302-2.247l-8.25-14.25a1.5 1.5 0 00-2.604 0l-8.25 14.25a1.5 1.5 0 001.302 2.247z"
          />
        </svg>
        <p className={cn("font-medium", size === "block" ? "text-base" : "text-sm")}>
          {displayMessage}
        </p>
      </div>

      <div className="flex items-center gap-3">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-sm font-medium text-red-700 transition-colors hover:bg-red-50"
          >
            ↻ Retry
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowDetails((v) => !v)}
          className="text-xs text-slate-500 underline-offset-2 hover:text-slate-700 hover:underline"
        >
          {showDetails ? "Hide details" : "Show details"}
        </button>
      </div>

      {showDetails && (
        <pre className="mt-1 max-w-full overflow-x-auto rounded bg-slate-900/90 px-3 py-2 text-left font-mono text-[11px] text-slate-100">
          {detail}
        </pre>
      )}
    </div>
  );
}
