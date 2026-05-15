"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

// Ask Neo is the protagonist — it owns / and sits first in the nav.
// No Dashboard: the home page already surfaces the briefing strip.
const NAV = [
  { href: "/",           label: "Ask Neo" },
  { href: "/insights",   label: "Insights" },
  { href: "/meetings",   label: "Meetings" },
  { href: "/votes",      label: "Votes" },
  { href: "/financials", label: "Financials" },
];

export default function Navbar() {
  const path = usePathname();
  return (
    <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/90 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-screen-2xl items-center gap-8 px-6">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-2 shrink-0">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-indigo-600 text-white font-bold text-sm">N</span>
          <span className="font-semibold text-slate-900 tracking-tight">Neo</span>
          <span className="text-xs text-slate-400 font-medium">v2</span>
        </Link>

        {/* Nav links */}
        <nav className="flex items-center gap-1">
          {NAV.map(({ href, label }) => {
            const active = href === "/" ? path === "/" : path.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  active
                    ? "bg-indigo-50 text-indigo-700"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                )}
              >
                {label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
