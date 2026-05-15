import Link from "next/link";
import BriefingCard, { type BriefingItem } from "./BriefingCard";

interface BriefingStripProps {
  items?: BriefingItem[];
  /** Optional section title override. */
  title?: string;
  /** Optional link shown on the right of the header. */
  moreHref?: string;
  moreLabel?: string;
}

/**
 * "This week across colleges" strip.
 * Renders a horizontally scrollable row of briefing cards on mobile,
 * a 2–3 column grid on desktop.
 *
 * NOTE: Until /briefing exists on the backend, pass `items` in explicitly.
 * When left undefined the strip renders nothing so it never ships empty scaffolding.
 */
export default function BriefingStrip({
  items,
  title = "This week across colleges",
  moreHref = "/insights",
  moreLabel = "Open the Insight Matrix →",
}: BriefingStripProps) {
  if (!items || items.length === 0) return null;

  return (
    <section>
      <div className="mb-3 flex items-end justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">{title}</h2>
          <p className="mt-0.5 text-xs text-slate-400">Cross-college signals pulled from recent board meetings.</p>
        </div>
        {moreHref && (
          <Link href={moreHref} className="text-sm text-indigo-600 hover:underline">
            {moreLabel}
          </Link>
        )}
      </div>

      {/* Mobile: horizontal scroll. Desktop: grid. */}
      <div className="-mx-6 overflow-x-auto px-6 pb-2 sm:mx-0 sm:overflow-visible sm:px-0">
        <div className="flex gap-3 sm:grid sm:grid-cols-2 sm:gap-4 lg:grid-cols-3">
          {items.map((item) => (
            <BriefingCard key={item.id} item={item} />
          ))}
        </div>
      </div>
    </section>
  );
}
