import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

const palette: Record<string, string> = {
  Grant: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
  RFP: "bg-indigo-500/15 text-indigo-600 dark:text-indigo-400 border-indigo-500/30",
  Tender: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
  Proposal: "bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30",
  Fellowship: "bg-fuchsia-500/15 text-fuchsia-600 dark:text-fuchsia-400 border-fuchsia-500/30",
  Award: "bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30",
  Challenge: "bg-orange-500/15 text-orange-600 dark:text-orange-400 border-orange-500/30",
  Other: "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400 border-zinc-500/30",
};

// Per-vertical colours so each vertical is recognisable at a glance.
// Matched by PREFIX: the canonical labels carry suffixes — "E4C(Evidence for
// Change)", "Climate/Sustainability(ESG)" — so exact-key lookup silently
// dropped those two to grey while the other four stayed coloured.
const verticalPalette: Array<[string, string]> = [
  ["Livelihood", "bg-lime-500/15 text-lime-700 dark:text-lime-400 border-lime-500/30"],
  ["Health", "bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30"],
  ["E4C", "bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30"],
  ["Climate/Sustainability", "bg-teal-500/15 text-teal-700 dark:text-teal-400 border-teal-500/30"],
  ["Worker Wellbeing", "bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30"],
  ["Innovative Finance", "bg-cyan-500/15 text-cyan-700 dark:text-cyan-400 border-cyan-500/30"],
];

function verticalTone(vertical: string): string {
  const hit = verticalPalette.find(([key]) => vertical.startsWith(key));
  return hit?.[1] ?? "bg-zinc-500/15 text-zinc-600 dark:text-zinc-400 border-zinc-500/30";
}

export function Badge({
  className,
  category,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { category?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
        palette[category ?? ""] ?? palette.Other,
        className
      )}
      {...props}
    />
  );
}

/** Small badge indicating an opportunity's canonical Vertical (e.g. Health, E4C). */
export function VerticalBadge({
  className,
  vertical,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { vertical: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium",
        verticalTone(vertical),
        className
      )}
      title={vertical}
      {...props}
    >
      {/* The parenthetical suffix is noise in a dense table — the full label is
          still available on hover. */}
      {vertical.replace(/\s*\((Evidence for Change|ESG)\)\s*$/i, "")}
    </span>
  );
}
