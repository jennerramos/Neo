import { notFound } from "next/navigation";
import { FEATURES } from "@/lib/features";
import VotesView from "./VotesView";

// Server-side gate for the switch in lib/features.ts. The nav renders "Votes"
// as an inert label when the section is off; this stops a typed URL from
// reaching a page the nav deliberately won't link to.
export default function VotesPage() {
  if (!FEATURES.votes) notFound();
  return <VotesView />;
}
