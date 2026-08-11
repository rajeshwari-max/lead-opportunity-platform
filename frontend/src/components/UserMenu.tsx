import { LogOut, Shield, User } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface Props {
  name: string;
  email: string;
  isAdmin: boolean;
  /** Hidden entirely when no password is configured — there is nobody to be
   *  signed in as, and a Logout button that does nothing is worse than none. */
  authRequired: boolean;
}

export function UserMenu({ name, email, isAdmin, authRequired }: Props) {
  if (!authRequired) return null;

  const initials = (name || email || "?")
    .split(/[\s@.]+/).filter(Boolean).slice(0, 2)
    .map((p) => p[0]?.toUpperCase()).join("");

  return (
    <div className="flex items-center gap-2">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
        {initials || <User className="h-3.5 w-3.5" />}
      </div>
      <div className="hidden min-w-0 leading-tight sm:block">
        <div className="flex items-center gap-1">
          <span className="truncate text-xs font-medium">{name || "Signed in"}</span>
          {isAdmin && (
            <span title="Admin — can start scrapes and edit team routing"
                  className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/15 px-1.5 text-[10px] font-semibold text-amber-500">
              <Shield className="h-2.5 w-2.5" /> admin
            </span>
          )}
        </div>
        <div className="truncate text-[11px] text-muted-foreground">{email}</div>
      </div>
      <Button
        size="sm" variant="ghost" title="Sign out"
        onClick={async () => { await api.logout(); window.location.reload(); }}
      >
        <LogOut className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
