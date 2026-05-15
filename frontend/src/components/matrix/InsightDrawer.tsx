"use client";
import { useEffect, useState } from "react";
import type { InsightCell, InsightDetail } from "@/types";
import { fetchInsightDetail } from "@/lib/api";
import { actionTypeColor, cn } from "@/lib/utils";
import Spinner from "@/components/ui/Spinner";
import EvidencePanel from "@/components/insights/EvidencePanel";

interface InsightDrawerProps {
  cell: InsightCell | null;
  onClose: () => void;
}

export default function InsightDrawer({ cell, onClose }: InsightDrawerProps) {
  const [detail, setDetail] = useState<InsightDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!cell) { setDetail(null); return; }
    setLoading(true);
    setError(null);
    fetchInsightDetail(cell.insight_id)
      .then(setDetail)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [cell?.insight_id]);

  if (!cell) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm animate-drawer-backdrop"
        onClick={onClose}
        aria-hidden
      />

      {/* Panel */}
      <aside
        className="fixed right-0 top-0 z-50 flex h-full w-full max-w-xl flex-col bg-white shadow-2xl animate-drawer"
        role="dialog"
        aria-label={cell.label}
      >
        {/* Header */}
        <div className="flex items-start justify-between border-b border-slate-200 px-6 py-4">
          <div className="pr-8">
            <div className="mb-1 flex items-center gap-2">
              <span className={cn(
                "rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide",
                actionTypeColor(cell.action_type)
              )}>
                {cell.action_type}
              </span>
              <span className="text-xs text-slate-400">{cell.theme_label}</span>
            </div>
            <h2 className="text-lg font-semibold leading-snug text-slate-900">{cell.label}</h2>
            <p className="mt-0.5 text-sm text-slate-500">{cell.school_name}</p>
            <p className="mt-2 text-[11px] text-slate-400">
              Use <kbd className="rounded bg-slate-100 px-1 py-0.5 font-mono">←</kbd>{" "}
              <kbd className="rounded bg-slate-100 px-1 py-0.5 font-mono">→</kbd> to step,{" "}
              <kbd className="rounded bg-slate-100 px-1 py-0.5 font-mono">Esc</kbd> to close.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            aria-label="Close"
          >
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {loading && (
            <div className="flex justify-center py-16">
              <Spinner size={28} />
            </div>
          )}
          {error && (
            <div className="rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</div>
          )}
          {detail && !loading && (
            <EvidencePanel detail={detail} variant="compact" showFullPageCta />
          )}
        </div>
      </aside>
    </>
  );
}
