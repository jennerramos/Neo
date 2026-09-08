/**
 * Section switches for the UI.
 *
 * A section that is switched off keeps its nav label — the shape of the
 * product stays visible — but the label doesn't navigate, in-page links to it
 * are dropped, and the route itself 404s if someone types the URL. Flipping a
 * value back to `true` is the only edit needed to bring a section online; no
 * markup elsewhere has to change.
 *
 * The API keeps serving /votes and /financials either way — this is a UI
 * switch, not an access control.
 */
export const FEATURES = {
  votes: false,
  financials: false,
} as const;

export type FeatureKey = keyof typeof FEATURES;

/** True when the named section is switched on. */
export function isEnabled(feature: FeatureKey): boolean {
  return FEATURES[feature];
}
