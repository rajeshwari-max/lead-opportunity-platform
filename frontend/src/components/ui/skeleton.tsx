import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

/** Pulsing placeholder shown while cards / charts / tables load. */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-lg bg-muted", className)} {...props} />;
}
