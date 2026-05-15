import { redirect } from "next/navigation";

/**
 * /ask is deprecated — Ask Neo now lives on the home page.
 * This redirect keeps any existing bookmarks / in-flight links working.
 */
export default function AskRedirect() {
  redirect("/");
}
