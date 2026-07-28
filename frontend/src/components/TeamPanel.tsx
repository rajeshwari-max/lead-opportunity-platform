import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Loader2, Mail, Pencil, Plus, Send, Trash2, Users } from "lucide-react";
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
    await api.deleteMember(m.id);
    load();
  };

  const sendNow = async (m: TeamMember) => {
    setSending(m.id);
    try {
      const res = await api.sendToMember(m.id);
      const body = await res.json();
      if (res.ok) {
        notify("ok", body.sent > 0
          ? `Sent ${body.sent} opportunit${body.sent === 1 ? "y" : "ies"} to ${m.name}`
          : `${m.name} has no new matches — nothing sent`);
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

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Users className="h-4 w-4" /> Team &amp; Lead Routing
        </CardTitle>
        <Button size="sm" variant="outline"
                onClick={() => { setEditingId(null); setForm(emptyForm); setShowForm(!showForm); }}>
          <Plus className="h-3.5 w-3.5" /> Add
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
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
        {members.map((m) => (
          <div key={m.id} className="flex items-center gap-2 rounded-lg border border-border p-2.5">
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{m.name}</p>
              <p className="truncate text-xs text-muted-foreground">
                <Mail className="mr-1 inline h-3 w-3" />
                {m.email}
                {m.keywords && <span className="ml-2 text-primary">· {m.keywords}</span>}
                {m.categories && <span className="ml-1">· {m.categories}</span>}
                {m.verticals && <span className="ml-1 text-accent">· {m.verticals}</span>}
              </p>
            </div>
            {matchCounts[m.id] != null && (
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${
                matchCounts[m.id] > 0 ? "bg-emerald-500/15 text-emerald-400" : "bg-muted text-muted-foreground"}`}>
                {matchCounts[m.id]} new
              </span>
            )}
            <Button size="sm" disabled={sending === m.id || !emailConfigured}
                    title={emailConfigured ? `Email new matches to ${m.name}` : "Configure SMTP first"}
                    onClick={() => sendNow(m)}>
              {sending === m.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              Send
            </Button>
            <Button size="sm" variant="ghost" onClick={() => startEdit(m)} title="Edit member">
              <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
            </Button>
            <Button size="sm" variant="ghost" onClick={() => remove(m)} title="Remove member">
              <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
            </Button>
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
    </Card>
  );
}
