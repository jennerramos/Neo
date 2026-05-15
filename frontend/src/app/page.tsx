import { fetchMeetings, fetchSchools } from "@/lib/api";
import AskBox from "@/components/ask/AskBox";
import BriefingStrip from "@/components/briefing/BriefingStrip";
import type { BriefingItem } from "@/components/briefing/BriefingCard";
import Link from "next/link";
import { fmtDate } from "@/lib/utils";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  // Best-effort data fetch — the home page must still render if the API is down.
  const [schoolsRes, meetingsRes] = await Promise.allSettled([
    fetchSchools(),
    fetchMeetings({ limit: 6 }),
  ]);

  const schools = schoolsRes.status === "fulfilled" ? schoolsRes.value : [];
  const recentMeetings = meetingsRes.status === "fulfilled" ? meetingsRes.value.meetings : [];

  // Placeholder briefing items derived from recent meetings until the backend
  // exposes a curated /briefing endpoint. Safe to leave empty — the strip
  // hides itself when there are no items.
  const briefing: BriefingItem[] = recentMeetings.slice(0, 6).map((m) => ({
    id: `meeting-${m.meeting_id}`,
    school_name: m.school_name ?? m.school_slug,
    school_slug: m.school_slug,
    headline: m.title ?? `Board meeting on ${fmtDate(m.published_date)}`,
    summary: undefined,
    date: m.published_date,
    href: `/meetings/${m.meeting_id}`,
  }));

  return (
    <div className="space-y-16">
      {/* Hero: Ask Neo */}
      <section className="pt-4 sm:pt-10">
        <AskBox
          hero
          schools={schools.map((s) => ({ slug: s.slug, name: s.name }))}
        />
      </section>

      {/* Briefing strip — cross-college signals */}
      <BriefingStrip items={briefing} />

      {/* Footer link into insights — low-key, not competing with Ask Neo */}
      <div className="flex items-center justify-center pb-6 text-sm text-slate-400">
        <Link
          href="/insights"
          className="rounded-full border border-slate-200 bg-white px-4 py-2 transition hover:border-indigo-300 hover:text-indigo-700"
        >
          Browse the cross-college insight matrix →
        </Link>
      </div>
    </div>
  );
}
