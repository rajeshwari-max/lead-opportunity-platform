import { useState } from "react";
import { Loader2, Lock, Mail, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";

/** Shown instead of the dashboard when a password is required and this browser
 *  doesn't have a session yet. */
export function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (res.ok) {
        onSuccess();
      } else {
        // 403 means the password was right but the email isn't on the team —
        // worth saying, because the fix is different (ask an admin to add you).
        const body = await res.json().catch(() => ({}));
        setError(res.status === 403 ? body.detail : "Incorrect email or password");
        setPassword("");
      }
    } catch {
      setError("Could not reach the server");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden p-6">
      {/* Two soft colour washes rather than a flat panel on a flat page. Pure
          decoration, so pointer-events are off and it never eats a click. */}
      <div aria-hidden
           className="pointer-events-none absolute -left-32 -top-32 h-96 w-96 rounded-full bg-primary/20 blur-3xl" />
      <div aria-hidden
           className="pointer-events-none absolute -bottom-32 -right-32 h-96 w-96 rounded-full bg-emerald-500/15 blur-3xl" />

      <form
        onSubmit={submit}
        className="relative w-full max-w-sm rounded-2xl border border-border/60 bg-card/80 p-8 shadow-2xl backdrop-blur"
      >
        <div className="mb-7 flex flex-col items-center text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-emerald-500 shadow-lg">
            <Zap className="h-6 w-6 text-white" />
          </div>
          <h1 className="text-lg font-semibold tracking-tight">Lead Scanning Platform</h1>
          <p className="mt-1 text-xs text-muted-foreground">
            Sign in with your work email
          </p>
        </div>

        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">Email</span>
            <div className="relative">
              <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="email" autoFocus value={email} placeholder="you@catalysts.org"
                onChange={(e) => setEmail(e.target.value)}
                className="h-11 w-full rounded-lg border border-border bg-background pl-10 pr-3 text-sm outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary focus:ring-2 focus:ring-primary/20"
              />
            </div>
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">Password</span>
            <div className="relative">
              <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="password" value={password} placeholder="••••••••"
                onChange={(e) => setPassword(e.target.value)}
                className="h-11 w-full rounded-lg border border-border bg-background pl-10 pr-3 text-sm outline-none transition-colors placeholder:text-muted-foreground/70 focus:border-primary focus:ring-2 focus:ring-primary/20"
              />
            </div>
          </label>
        </div>

        {/* Reserved height, so the form doesn't jump when an error appears. */}
        <div className="min-h-[1.25rem] pt-2">
          {error && <p className="text-xs text-red-500">{error}</p>}
        </div>

        <Button type="submit" className="h-11 w-full text-sm" disabled={busy || !password || !email}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
