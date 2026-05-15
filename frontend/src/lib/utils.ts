/** Utility helpers */

export function fmtCurrency(amount: number | null | undefined): string {
  if (amount == null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(amount);
}

export function fmtDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function fmtConfidence(score: number | null | undefined): string {
  if (score == null) return "—";
  return `${Math.round(score * 100)}%`;
}

/** Action type → Tailwind badge colour */
export function actionTypeColor(action: string): string {
  const map: Record<string, string> = {
    approved:  "bg-emerald-100 text-emerald-800",
    launched:  "bg-blue-100 text-blue-800",
    proposed:  "bg-amber-100 text-amber-800",
    discussed: "bg-slate-100 text-slate-700",
    continued: "bg-purple-100 text-purple-800",
  };
  return map[action?.toLowerCase()] ?? "bg-slate-100 text-slate-700";
}

/** Action type → solid accent colour (for bars, dots, left-borders). */
export function actionTypeBar(action: string): string {
  const map: Record<string, string> = {
    approved:  "bg-emerald-400",
    launched:  "bg-blue-400",
    proposed:  "bg-amber-400",
    discussed: "bg-slate-300",
    continued: "bg-purple-400",
  };
  return map[action?.toLowerCase()] ?? "bg-slate-300";
}

/** Action type → left-border utility class (matches actionTypeBar colours). */
export function actionTypeBorder(action: string): string {
  const map: Record<string, string> = {
    approved:  "border-l-emerald-400",
    launched:  "border-l-blue-400",
    proposed:  "border-l-amber-400",
    discussed: "border-l-slate-300",
    continued: "border-l-purple-400",
  };
  return map[action?.toLowerCase()] ?? "border-l-slate-300";
}

/** Theme key → accent colour for matrix header */
export function themeColor(key: string): string {
  const map: Record<string, string> = {
    T1: "bg-sky-600",
    T2: "bg-teal-600",
    T3: "bg-indigo-600",
    T4: "bg-violet-600",
    T5: "bg-rose-600",
    T6: "bg-amber-600",
    T7: "bg-slate-500",
  };
  return map[key] ?? "bg-slate-500";
}

export function cn(...classes: (string | false | undefined | null)[]): string {
  return classes.filter(Boolean).join(" ");
}

/**
 * Turn an exception from the API client into a trustee-friendly message
 * plus the raw technical detail (kept behind a toggle in the UI).
 *
 * The API client throws `Error("API <status>: <body>")` for non-2xx and a
 * native TypeError("Failed to fetch") when the request never reached the
 * server. We pattern-match those shapes; anything else falls through to a
 * generic message.
 */
export function humanizeApiError(err: unknown): { message: string; detail: string } {
  const raw = err instanceof Error ? err.message : String(err);

  // Network-level failure (server down, CORS, offline).
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return {
      message: "Can't reach the server. Check your connection and try again.",
      detail: raw,
    };
  }

  // HTTP error from our client: "API <status>: <body>"
  const m = raw.match(/^API (\d{3}):/);
  if (m) {
    const status = Number(m[1]);
    if (status >= 500) {
      return {
        message: "Something went wrong on our side. Please try again in a moment.",
        detail: raw,
      };
    }
    if (status === 404) {
      return { message: "We couldn't find what you were looking for.", detail: raw };
    }
    if (status === 401 || status === 403) {
      return { message: "You don't have access to this data.", detail: raw };
    }
    if (status === 400 || status === 422) {
      return { message: "That request didn't look right — try different filters.", detail: raw };
    }
  }

  return { message: "We couldn't load this section.", detail: raw };
}
