import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, X } from "lucide-react";

interface Props {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  /** Show a search box once the list is longer than this. */
  searchAfter?: number;
  className?: string;
}

/** A compact multi-select for the table toolbar.
 *
 *  Deliberately not a native <select multiple>: that renders as a scrolling
 *  list box which cannot show how many things are picked without taking four
 *  lines of vertical space, and ctrl-clicking to deselect is not something
 *  most people discover.
 *
 *  The button carries the state — "Source: 2" — so the current filter is
 *  readable without opening the menu.
 */
export function MultiSelect({
  label, options, selected, onChange, searchAfter = 8, className = "",
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const box = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape. Without this the menu stays open
  // behind whatever you click next, which reads as the UI being stuck.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = (opt: string) =>
    onChange(selected.includes(opt) ? selected.filter((s) => s !== opt) : [...selected, opt]);

  const shown = query
    ? options.filter((o) => o.toLowerCase().includes(query.toLowerCase()))
    : options;

  return (
    <div ref={box} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs transition-colors ${
          selected.length
            ? "border-primary bg-primary/10 text-foreground"
            : "border-border hover:bg-muted"
        }`}
      >
        <span>{label}</span>
        {selected.length > 0 && (
          <span className="rounded-full bg-primary px-1.5 text-[10px] font-semibold text-primary-foreground">
            {selected.length}
          </span>
        )}
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {selected.length > 0 && (
        <button
          type="button"
          title={`Clear ${label.toLowerCase()} filter`}
          onClick={(e) => {
            e.stopPropagation();
            onChange([]);
          }}
          className="absolute -right-1 -top-1 rounded-full bg-muted p-0.5 text-muted-foreground hover:bg-red-500/20 hover:text-red-500"
        >
          <X className="h-2.5 w-2.5" />
        </button>
      )}

      {open && (
        <div className="absolute right-0 z-50 mt-1 w-60 rounded-lg border border-border bg-background p-1.5 shadow-lg">
          {options.length > searchAfter && (
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`Search ${label.toLowerCase()}…`}
              className="mb-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs outline-none focus:border-primary"
            />
          )}
          <div className="max-h-64 overflow-y-auto">
            {shown.length === 0 && (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">No matches</p>
            )}
            {shown.map((opt) => {
              const on = selected.includes(opt);
              return (
                <button
                  key={opt}
                  type="button"
                  onClick={() => toggle(opt)}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-muted"
                >
                  <span
                    className={`flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border ${
                      on ? "border-primary bg-primary" : "border-border"
                    }`}
                  >
                    {on && <Check className="h-2.5 w-2.5 text-primary-foreground" />}
                  </span>
                  <span className="flex-1 truncate" title={opt}>{opt}</span>
                </button>
              );
            })}
          </div>
          {selected.length > 0 && (
            <button
              type="button"
              onClick={() => onChange([])}
              className="mt-1 w-full rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              Clear all
            </button>
          )}
        </div>
      )}
    </div>
  );
}
