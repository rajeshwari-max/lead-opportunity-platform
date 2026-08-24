import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, ChevronDown, Loader2, Mail, Pencil, Plus, RotateCcw, Search, Send, Trash2, Users, X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { VERTICALS, type TeamMember } from "@/lib/types";

const emptyForm = { name: "", email: "", keywords: "", categories: "", verticals: "", auto_send: true, active: true };

export function TeamPanel({ readOnly = false }: { readOnly?: boolean }) {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [matchCounts, setMatchCounts] = useState<Record<number, number>>({});
  const [emailConfigured, setEmailConfigured] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [sending, setSending] = useState<number | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [categoryOptions, setCategoryOptions] = useState<string[]>([]);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const notify = (kind: "ok" | "err", text: string) => {
    setToast({ kind, text });
    setTimeout(() => setToast(null), 5000);
  };

  const load = useCallback(async () => {
    try {
      const [team, status, facets] = await Promise.all([
        api.team(),
        api.emailStatus(),
        api.facets(),
      ]);
      setMembers(team);
      setEmailConfigured(status.configured);
      setCategoryOptions(facets.categories.filter((c) => c !== "Other"));
      // Preview how many NEW opportunities each member would receive
      const counts: Record<number, number> = {};
      await Promise.all(
        team.map(async (m) => {
          counts[m.id] = (await api.memberMatches(m.id)).length;
        })
      );
      setMatchCounts(counts);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const saveMember = async () => {
    if (!form.name.trim() || !form.email.includes("@")) {
      notify("err", "Name and a valid email are required");
      return;
    }
    const res = editingId != null
      ? await api.updateMember(editingId, form)
      : await api.addMember(form);
    if (res.ok) {
      setForm(emptyForm);
      setShowForm(false);
      setEditingId(null);
      notify("ok", editingId != null ? "Member updated" : "Team member added");
      load();
    } else {
      notify("err", (await res.json()).detail ?? "Could not save member");
    }
  };

  const startEdit = (m: TeamMember) => {
    setEditingId(m.id);
    setForm({ name: m.name, email: m.email, keywords: m.keywords,
              categories: m.categories, verticals: m.verticals ?? "",
              auto_send: m.auto_send, active: m.active });
    setShowForm(true);
  };

  const remove = async (m: TeamMember) => {
    // Deleting a member also loses their send history, so it is worth a pause.
    if (!window.confirm(`Remove ${m.name} (${m.email}) from lead routing?`)) return;
    try {
      const res = await api.deleteMember(m.id);
      // The response was previously ignored entirely: a 403, a 405 from a proxy
      // that disallows DELETE, or a 404 all looked identical to success — the
      // row simply stayed put with no explanation.
      if (res.ok || res.status === 204) {
        notify("ok", `Removed ${m.name}`);
      } else {
        notify("err", `Could not remove ${m.name} — server said ${res.status}`);
      }
    } catch {
      notify("err", "Could not remove — is the backend reachable?");
    } finally {
      load();
    }
  };

  const sendNow = async (m: TeamMember, resend = false) => {
    // Resending mails everything currently matching, not just what's new, so
    // it is worth confirming — the recipient sees a full digest again.
    if (resend && !window.confirm(
      `Resend the full current digest to ${m.name} (${m.email})?\n\n` +
      "This includes opportunities already sent before, so they receive the " +
      "complete up-to-date list rather than only what's new."
    )) return;

    setSending(m.id);
    try {
      const res = await api.sendToMember(m.id, resend);
      const body = await res.json();
      if (res.ok) {
        const verb = body.resent ? "Resent" : "Sent";
        notify("ok", body.sent > 0
          ? `${verb} ${body.sent} opportunit${body.sent === 1 ? "y" : "ies"} to ${m.name}`
          : body.detail ?? `${m.name} has no new matches — nothing sent`);
        load();
      } else {
        notify("err", body.detail ?? "Send failed");
      }
    } catch {
      notify("err", "Send failed — is the backend running?");
    } finally {
      setSending(null);
    }
  };

  const toggleCategory = (cat: string) => {
    const set = new Set(form.categories.split(",").map((c) => c.trim()).filter(Boolean));
    set.has(cat) ? set.delete(cat) : set.add(cat);
    setForm({ ...form, categories: [...set].join(", ") });
  };

  const toggleVertical = (vertical: string) => {
    const set = new Set(form.verticals.split(",").map((s) => s.trim()).filter(Boolean));
    set.has(vertical) ? set.delete(vertical) : set.add(vertical);
    setForm({ ...form, verticals: [...set].join(", ") });
  };

  // Collapsed by default once the team grows: the panel renders every member
  // with their keyword, category and vertical rules, which runs to several
  // screens and pushes the Expert Pool below the fold.
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem("lop-team-collapsed") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("lop-team-collapsed", collapsed ? "1" : "0"); } catch { /* ignore */ }
  }, [collapsed]);

  // Bulk Auto/Manual. Turning everyone ON is a two-step confirm: fetch the real
  // per-member counts, show them, then apply. Turning everyone OFF applies
  // immediately — it can only reduce what gets sent, so there is nothing to warn
  // about and a confirmation would just be friction.
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkPreview, setBulkPreview] = useState<
    Awaited<ReturnType<typeof api.autoSendPreview>> | null
  >(null);

  const askAllAuto = async () => {
    setBulkBusy(true);
    try {
      setBulkPreview(await api.autoSendPreview());
    } catch {
      setToast({ kind: "err", text: "Could not read the pending counts" });
    } finally {
      setBulkBusy(false);
    }
  };

  const applyAll = async (value: boolean) => {
    setBulkBusy(true);
    setBulkPreview(null);
    try {
      const r = await api.setAutoSendAll(value);
      setToast({
        kind: "ok",
        text: `${r.changed} member(s) set to ${value ? "Auto" : "Manual"}.`,
      });
      load();
    } catch (e) {
      setToast({ kind: "err", text: e instanceof Error ? e.message : "Update failed" });
    } finally {
      setBulkBusy(false);
    }
  };

  const [query, setQuery] = useState("");
  const shown = query.trim()
    ? members.filter((m) => {
        const q = query.trim().toLowerCase();
        // Search the routing rules too, not just the person — "who is getting
        // the climate leads?" is the question this panel is usually opened for.
        return [m.name, m.email, m.keywords, m.categories, m.verticals]
          .some((v) => (v || "").toLowerCase().includes(q));
      })
    : members;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <button type="button" onClick={() => setCollapsed((v) => !v)}
                  title={collapsed ? "Expand" : "Collapse"}
                  className="flex items-center gap-2 hover:text-primary">
            <ChevronDown className={`h-4 w-4 transition-transform ${collapsed ? "-rotate-90" : ""}`} />
            <Users className="h-4 w-4" /> Team &amp; Lead Routing
          </button>
          <span className="text-xs font-normal text-muted-foreground">
            {members.length}
          </span>
        </CardTitle>
        <Button size="sm" variant="outline"
                onClick={() => {
                  setCollapsed(false);          // adding while collapsed hid the form
                  setEditingId(null); setForm(emptyForm); setShowForm(!showForm);
                }}>
          <Plus className="h-3.5 w-3.5" /> Add
        </Button>
      </CardHeader>
      {!collapsed && (
      <CardContent className="space-y-3">
        {members.length > 3 && (
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name, email, keyword or vertical…"
              className="h-8 w-full rounded-md border border-border bg-transparent pl-8 pr-7 text-xs outline-none focus:border-primary"
            />
            {query && (
              <button type="button" onClick={() => setQuery("")}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground">
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        )}
        {/* Bulk Auto / Manual. Turning everyone ON is confirmed against real
            counts first, because a member with no keywords matches EVERY
            opportunity and a sent email cannot be recalled. */}
        {!readOnly && members.length > 0 && (
          <div className="space-y-1.5 rounded-lg border border-border p-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">Everyone</span>
              <span className="text-[11px] text-muted-foreground">
                {members.filter((m) => m.auto_send).length}/{members.length} on Auto
              </span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <Button size="sm" variant="outline" className="h-7 text-xs"
                      disabled={bulkBusy}
                      onClick={askAllAuto}>
                {bulkBusy ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                All Auto
              </Button>
              <Button size="sm" variant="outline" className="h-7 text-xs"
                      disabled={bulkBusy}
                      onClick={() => applyAll(false)}>
                All Manual
              </Button>
            </div>
            {bulkPreview && (
              <div className="space-y-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 p-2">
                <p className="text-xs font-medium text-amber-500">
                  This will queue {bulkPreview.total_pending.toLocaleString()} email rows
                  at the next scheduled send.
                </p>
                <ul className="max-h-32 space-y-0.5 overflow-y-auto text-[11px] text-muted-foreground">
                  {bulkPreview.members.filter((m) => m.pending > 0).map((m) => (
                    <li key={m.id}>
                      {m.name}: <b>{m.pending.toLocaleString()}</b>
                      {m.no_filters && (
                        <span className="text-amber-500"> — no keywords, matches everything</span>
                      )}
                    </li>
                  ))}
                </ul>
                <div className="flex gap-1.5">
                  <Button size="sm" className="h-7 text-xs" onClick={() => applyAll(true)}>
                    Yes, turn all on
                  </Button>
                  <Button size="sm" variant="outline" className="h-7 text-xs"
                          onClick={() => setBulkPreview(null)}>
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {!emailConfigured && !readOnly && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              Email is not configured yet. Copy <b>backend/.env.example</b> to <b>backend/.env</b>,
              add your Gmail App Password, and restart the backend.
            </span>
          </div>
        )}

        {/* Add form */}
        <AnimatePresence>
          {showForm && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }} className="space-y-2 overflow-hidden rounded-lg border border-border p-3">
              <Input placeholder="Name (e.g. Priya Sharma)" value={form.name}
                     onChange={(e) => setForm({ ...form, name: e.target.value })} />
              <Input placeholder="Email" type="email" value={form.email}
                     onChange={(e) => setForm({ ...form, email: e.target.value })} />
              <Input placeholder="Interest keywords, comma-separated (e.g. climate, water)"
                     value={form.keywords}
                     onChange={(e) => setForm({ ...form, keywords: e.target.value })} />
              <div className="flex flex-wrap gap-1.5">
                {categoryOptions.map((c) => {
                  const on = form.categories.includes(c);
                  return (
                    <button key={c} onClick={() => toggleCategory(c)}
                            className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
                              on ? "border-primary bg-primary/20 text-primary"
                                 : "border-border text-muted-foreground hover:bg-muted"}`}>
                      {c}
                    </button>
                  );
                })}
              </div>
              {/* Vertical routing: only opportunities in the selected verticals are emailed */}
              <p className="pt-1 text-[11px] font-semibold text-muted-foreground">Vertical</p>
              <div className="flex flex-wrap gap-1.5">
                {VERTICALS.map((s) => {
                  const on = form.verticals.includes(s);
                  return (
                    <button key={s} onClick={() => toggleVertical(s)}
                            className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
                              on ? "border-accent bg-accent/20 text-accent"
                                 : "border-border text-muted-foreground hover:bg-muted"}`}>
                      {s}
                    </button>
                  );
                })}
              </div>
              <p className="text-[11px] text-muted-foreground">
                Nothing selected = all categories / all verticals. Auto-digest after each scrape is on by default.
              </p>
              <div className="flex gap-2">
                <Button size="sm" onClick={saveMember}>
                  {editingId != null ? "Update member" : "Save member"}
                </Button>
                <Button size="sm" variant="ghost"
                        onClick={() => { setShowForm(false); setEditingId(null); setForm(emptyForm); }}>
                  Cancel
                </Button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Member list */}
        {members.length === 0 && !showForm && (
          <p className="text-sm text-muted-foreground">
            No team members yet. Add one with keywords like “climate” and click Send —
            they'll get a formatted email with every matching active opportunity.
          </p>
        )}
        {query && shown.length === 0 && (
          <p className="py-3 text-center text-xs text-muted-foreground">
            No team member matches "{query}".
          </p>
        )}
        {shown.map((m) => (
          // Two rows, not one. Four buttons and a count chip on a single line
          // left the name with no width at all in this sidebar — every member
          // rendered as an anonymous row of controls. Identity goes first and
          // gets the full width; the controls sit underneath and wrap.
          <div key={m.id} className="space-y-2 rounded-lg border border-border p-2.5">
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium" title={m.name}>{m.name}</p>
                <p className="truncate text-xs text-muted-foreground" title={m.email}>
                  <Mail className="mr-1 inline h-3 w-3" />
                  {m.email}
                </p>
                {(m.keywords || m.categories || m.verticals) && (
                  <p className="truncate text-xs text-muted-foreground">
                    {m.keywords && <span className="text-primary">{m.keywords}</span>}
                    {m.categories && <span className="ml-1">· {m.categories}</span>}
                    {m.verticals && <span className="ml-1 text-accent">· {m.verticals}</span>}
                  </p>
                )}
              </div>
              {/* Whether the automatic daily email includes this person. Off
                  means they only ever receive a manual Send. */}
              <button
                onClick={() => api.updateMember(m.id, { ...m, auto_send: !m.auto_send }).then(load)}
                title={m.auto_send
                  ? "Included in the automatic daily email — click to make manual-only"
                  : "Manual sends only — click to include in the automatic daily email"}
                className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${
                  m.auto_send ? "bg-primary/20 text-primary" : "bg-muted text-muted-foreground"}`}
              >
                {m.auto_send ? "Auto" : "Manual"}
              </button>
              {matchCounts[m.id] != null && (
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${
                  matchCounts[m.id] > 0 ? "bg-emerald-500/15 text-emerald-400" : "bg-muted text-muted-foreground"}`}>
                  {matchCounts[m.id]} new
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Button size="sm" disabled={sending === m.id || !emailConfigured}
                      title={emailConfigured ? `Email new matches to ${m.name}` : "Configure SMTP first"}
                      onClick={() => sendNow(m)}>
                {sending === m.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                Send
              </Button>
              {/* Send only ever mails what's new. Once someone has received an
                  opportunity it is skipped forever, so an improved email could
                  never reach them without this. */}
              <Button size="sm" variant="outline" disabled={sending === m.id || !emailConfigured}
                      title={emailConfigured
                        ? `Resend the full current digest to ${m.name}, including items already sent`
                        : "Configure SMTP first"}
                      onClick={() => sendNow(m, true)}>
                <RotateCcw className="h-3.5 w-3.5" />
                Resend
              </Button>
              <div className="ml-auto flex items-center gap-1">
                <Button size="sm" variant="ghost" onClick={() => startEdit(m)} title="Edit member">
                  <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
                </Button>
                <Button size="sm" variant="ghost" onClick={() => remove(m)} title="Remove member">
                  <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
                </Button>
              </div>
            </div>
          </div>
        ))}

        {/* Toast */}
        <AnimatePresence>
          {toast && (
            <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
                        className={`rounded-lg px-3 py-2 text-xs font-medium ${
                          toast.kind === "ok" ? "bg-emerald-500/15 text-emerald-400"
                                              : "bg-red-500/15 text-red-400"}`}>
              {toast.text}
            </motion.div>
          )}
        </AnimatePresence>
      </CardContent>
      )}
    </Card>
  );
}