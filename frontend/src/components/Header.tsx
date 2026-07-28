import { useEffect, useState } from "react";
import { Download, Moon, RefreshCw, Search, Sun, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { FilterState, Stats } from "@/lib/types";

interface Props {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  onRefresh: () => void;
  stats: Stats | null;
}

const THEME_KEY = "lop-theme";

export function Header({ filters, onChange, onRefresh, stats }: Props) {
  // Light mode is the default; dark stays available as an explicit opt-in toggle.
  const [dark, setDark] = useState(() => localStorage.getItem(THEME_KEY) === "dark");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  }, [dark]);

  return (
    <header className="sticky top-0 z-20 flex flex-wrap items-center gap-3 border-b border-border bg-background/80 px-6 py-3 backdrop-blur">
      <div className="flex items-center gap-2">
        <Zap className="h-5 w-5 text-primary" />
        <h1 className="text-base font-bold tracking-tight">Lead Scanning Platform</h1>
      </div>

      <div className="relative mx-auto w-full max-w-md flex-1">
        <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          className="pl-9"
          placeholder="Search all opportunities… (e.g. health)"
          value={filters.search}
          onChange={(e) => onChange({ ...filters, search: e.target.value, page: 1 })}
        />
      </div>

      <div className="flex items-center gap-2">
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
