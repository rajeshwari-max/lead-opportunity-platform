import { useEffect, useState } from "react";
import { Download, Moon, RefreshCw, Search, Sun, X, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { FilterState, Stats } from "@/lib/types";

interface Props {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  onRefresh: () => void;
  stats: Stats | null;
  /** Signed-in user badge + logout. Null when no password is configured. */
  userMenu?: React.ReactNode;
}

const THEME_KEY = "lop-theme";

export function Header({ filters, onChange, onRefresh, stats, userMenu }: Props) {
  // Light mode is the default; dark stays available as an explicit opt-in toggle.
  const [dark, setDark] = useState(() => localStorage.getItem(THEME_KEY) === "dark");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  }, [dark]);

  return (
    <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-border/70 bg-background/70 px-6 py-3 backdrop-blur-xl supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-2.5">
        {/* Gradient mark — the bare icon read as an afterthought next to the
            wordmark and gave the header no anchor. */}
        <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-accent shadow-sm">
          <Zap className="h-4 w-4 text-white" strokeWidth={2.4} />
        </span>
        <h1 className="text-base font-bold tracking-tight">Lead Scanning Platform</h1>
      </div>

      <div className="group relative mx-auto w-full max-w-md flex-1">
        <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground transition-colors group-focus-within:text-primary" />
        <Input
          className="pl-9 pr-8 transition-shadow focus:shadow-[0_0_0_4px_hsl(var(--primary)/0.10)]"
          placeholder="Search all opportunities… (e.g. health)"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value, page: 1 })}
        />
        {filters.search && (
          <button
            onClick={() => onChange({ ...filters, search: "", page: 1 })}
            title="Clear search"
            className="absolute right-2.5 top-2 rounded-md p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <div className="flex items-center gap-2">
        {userMenu}
        {stats?.last_scraped && (
          <span className="hidden text-xs text-muted-foreground md:block" title="Updated after every successful scrape">
            Last updated: {new Date(stats.last_scraped + "Z").toLocaleString()}
          </span>
        )}
        <a href={api.exportUrl("csv", filters)} download>
          <Button variant="outline" size="sm"><Download className="h-3.5 w-3.5" /> CSV</Button>
        </a>
        <a href={api.exportUrl("xlsx", filters)} download>
          <Button variant="outline" size="sm"><Download className="h-3.5 w-3.5" /> Excel</Button>
        </a>
        <Button variant="outline" size="icon" onClick={onRefresh} title="Clear filters & refresh — shows the original totals">
          <RefreshCw className="h-4 w-4" />
        </Button>
        <Button variant="outline" size="icon" onClick={() => setDark(!dark)} title="Toggle theme">
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </div>
    </header>
  );
}
