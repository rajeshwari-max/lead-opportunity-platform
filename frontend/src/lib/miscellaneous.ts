import type { Miscellaneous, MiscSector } from "./types";

// One wording for the Miscellaneous label everywhere it appears — the user
// dashboard, the admin table and the admin review card — so a row is never
// called two different things on two screens.

const shortVertical = (s: string) => s.replace(/\(.*\)/, "").trim();

export const miscName = (s: MiscSector) => s.kind === "vertical" ? shortVertical(s.name) : s.name;

/** "Miscellaneous — Health + Setu" when the strongest near misses, added
 *  strongest first, reach 100% of threshold; plain "Miscellaneous" otherwise. */
export const miscLabel = (m: Miscellaneous) =>
  m.reached ? `Miscellaneous — ${m.combined.map(miscName).join(" + ")}` : "Miscellaneous";

/** "Health 50% · Setu 45%" — every near miss and its share of its threshold. */
export const miscTitle = (m: Miscellaneous) =>
  m.sectors.length ? m.sectors.map(s => `${miscName(s)} ${s.pct}%`).join(" · ") : "No vertical or brand signal at all";
