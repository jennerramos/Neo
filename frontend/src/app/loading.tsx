import Spinner from "@/components/ui/Spinner";

/**
 * Default loading state for any server-rendered route segment.
 * Shown while the server component fetches data. Kept minimal —
 * pages with distinctive loading shapes can ship their own loading.tsx.
 */
export default function RootLoading() {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-32 text-slate-400">
      <Spinner size={32} />
      <p className="text-sm">Loading…</p>
    </div>
  );
}
