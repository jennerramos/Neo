/** Utility helpers */

export function fmtCurrency(amount: number | null | undefined): string {
  if (amount == null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(amount);
}

/**
 * Format a meeting date for display.
 *
 * The API sends calendar dates as "YYYY-MM-DD". `new Date("2026-02-27")`
 * parses that as *UTC midnight*, and `toLocaleDateString` then renders it in
 * the viewer's zone — so every viewer west of UTC, which is every NEO user,
 * saw each board meeting dated one day early. Date-only strings are split and
 * rebuilt as a local date so a meeting keeps the date it happened on.
 * Timestamps that carry a zone are left to the normal parser.
 */
export function fmtDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr.trim());
  const d = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(dateStr);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
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

// The extractor emits seven action types (initiative_extractor._ACTION_TYPES).
// The maps below cover all seven. They used to stop at five, so "other" and
// "cancelled" fell through the ?? and rendered in the same grey as
// "discussed" — a cancelled item was indistinguishable from a debated one,
// and neither appeared in the page legend.
const ACTION_BADGE: Record<string, string> = {
  approved:  "bg-emerald-100 text-emerald-800",
  launched:  "bg-blue-100 text-blue-800",
  proposed:  "bg-amber-100 text-amber-800",
  discussed: "bg-slate-100 text-slate-700",
  continued: "bg-purple-100 text-purple-800",
  cancelled: "bg-red-100 text-red-800",
  other:     "bg-stone-100 text-stone-700",
};

const ACTION_ACCENT: Record<string, string> = {
  approved:  "bg-emerald-400",
  launched:  "bg-blue-400",
  proposed:  "bg-amber-400",
  discussed: "bg-slate-300",
  continued: "bg-purple-400",
  cancelled: "bg-red-400",
  other:     "bg-stone-400",
};

const ACTION_BORDER: Record<string, string> = {
  approved:  "border-l-emerald-400",
  launched:  "border-l-blue-400",
  proposed:  "border-l-amber-400",
  discussed: "border-l-slate-300",
  continued: "border-l-purple-400",
  cancelled: "border-l-red-400",
  other:     "border-l-stone-400",
};

/** Action type → Tailwind badge colour */
export function actionTypeColor(action: string): string {
  return ACTION_BADGE[action?.toLowerCase()] ?? "bg-slate-100 text-slate-700";
}

/** Action type → solid accent colour (for bars, dots, left-borders). */
export function actionTypeBar(action: string): string {
  return ACTION_ACCENT[action?.toLowerCase()] ?? "bg-slate-300";
}

/** Action type → left-border utility class (matches actionTypeBar colours). */
export function actionTypeBorder(action: string): string {
  return ACTION_BORDER[action?.toLowerCase()] ?? "border-l-slate-300";
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
    T7: "bg-slate-600",
    T8: "bg-slate-500",
  };
  return map[key] ?? "bg-slate-500";
}

/**
 * Evidence strength → badge colour.
 *
 * This replaces the confidence percentage on the Insights page. The old badge
 * read "100% confidence" but the number measured extraction form-completeness,
 * not whether the claim held, so it invited exactly the reading it could not
 * support. These three states are each defensible from the record.
 */
export function evidenceLevelColor(level: string): string {
  const map: Record<string, string> = {
    measured:   "bg-emerald-100 text-emerald-800",
    action:     "bg-blue-100 text-blue-800",
    discussion: "bg-slate-100 text-slate-600",
  };
  return map[level] ?? "bg-slate-100 text-slate-600";
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
