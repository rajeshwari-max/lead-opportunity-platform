import { useEffect, useState } from "react";
import { AccountManagement } from "./AccountManagement";
import "./dashboard-leads.css";

type Lead = {id:number; opportunity:{id:number; title:string; deadline:string|null}; stage:string; notes:string; factors:string[]; next_action:string; next_action_date:string|null; updated_at:string};
type Activity = {viewed:number;reviewed:number;source_opened:number;recent:{opportunity:{id:number;title:string};viewed_at:string|null;reviewed_at:string|null;source_opened_at:string|null}[]};
type Person = {email:string;name:string;active:boolean;leads:Lead[];activity:Activity};
type HistoryEvent = {id:number;stage:string;note:string;created_at:string};
const stages = ["Saved","Preparing","Applied","Shortlisted","Accepted","Unsuccessful","Withdrawn"];
async function request(path:string,method="GET",body?:unknown) {
 const r=await fetch("/api"+path,{method,headers:body?{"Content-Type":"application/json"}:undefined,body:body?JSON.stringify(body):undefined});
 const data=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(typeof data.detail==="string"?data.detail:"Request failed. Please try again.");
 return data;
}

export async function recordLeadActivity(id:number,action:"viewed"|"reviewed"|"source_opened") {
 await request("/my-leads/activity","POST",{opportunity_id:id,action});
 window.dispatchEvent(new Event("saved-leads-changed"));
}
export function ReviewLeadButton({id}:{id:number}) {
 const [message,setMessage]=useState(""),[busy,setBusy]=useState(false);
 useEffect(()=>setMessage(""),[id]);
 return <span><button type="button" className="ud-control" disabled={busy} onClick={async()=>{setBusy(true);try{await recordLeadActivity(id,"reviewed");setMessage("Marked as reviewed");}catch{setMessage("Could not save review. Please try again.");}finally{setBusy(false);}}}>Mark reviewed</button><small role="status"> {message}</small></span>;
}

export function SaveLeadButton({id}:{id:number}) {
 const [busy,setBusy]=useState(false),[message,setMessage]=useState("");
 useEffect(()=>setMessage(""),[id]);
 return <span><button type="button" className="dl-save-button" disabled={busy} onClick={async()=>{setBusy(true);setMessage("");try{await request("/my-leads","POST",{opportunity_id:id});setMessage("Saved to My leads");window.dispatchEvent(new Event("saved-leads-changed"));}catch(e){setMessage(e instanceof Error?e.message:"Could not save");}finally{setBusy(false);}}}>{busy?"Saving…":message==="Saved to My leads"?"✓ Saved":"Save lead"}</button><small role="status"> {message}</small></span>;
}

export function DashboardLeads({isAdmin=false,name=""}:{isAdmin?:boolean;name?:string}) {
 const [open,setOpen]=useState(location.hash==="#my-leads"),[leads,setLeads]=useState<Lead[]>([]),[selected,setSelected]=useState<Lead|null>(null),[team,setTeam]=useState<Person[]>([]),[events,setEvents]=useState<HistoryEvent[]>([]);
 const [activity,setActivity]=useState<Activity>({viewed:0,reviewed:0,source_opened:0,recent:[]});
 const [error,setError]=useState(""),[notice,setNotice]=useState(""),[busy,setBusy]=useState(false),[current,setCurrent]=useState(""),[password,setPassword]=useState(""),[confirm,setConfirm]=useState("");
 async function run(fn:()=>Promise<void>){setError("");setNotice("");setBusy(true);try{await fn();}catch(e){setError(e instanceof Error?e.message:"Request failed");}finally{setBusy(false);}}
 const reload=async()=>{const [saved,stats]=await Promise.all([request("/my-leads"),request("/my-leads/activity")]);setLeads(saved);setActivity(stats);};
 useEffect(()=>{void run(reload);const listener=()=>void run(reload);const hash=()=>{if(location.hash==="#my-leads")setOpen(true);};window.addEventListener("hashchange",hash);window.addEventListener("saved-leads-changed",listener);return()=>{window.removeEventListener("saved-leads-changed",listener);window.removeEventListener("hashchange",hash);};},[]);
 async function choose(lead:Lead){setSelected(lead);setEvents(await request(`/my-leads/${lead.id}/history`));}
 return <section className="dl-panel" id="my-leads">
 <button className="dl-toggle" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?"▾":"▸"} My leads <span>{leads.length} saved · {activity.viewed} viewed · {activity.reviewed} reviewed</span><span className="dl-toggle-end">{open?"Close":"Continue where you left off"}</span></button>
 {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
 {open&&<><h3>{name?`${name.split(" ")[0]}’s activity`:"Your activity"}</h3><p>{activity.source_opened} source links opened · {leads.filter(l=>["Applied","Shortlisted","Accepted"].includes(l.stage)).length} saved leads at Applied, Shortlisted or Accepted.</p><p>Counts are distinct opportunities, not repeated clicks. Viewed means you opened a brief; reviewed means you selected Mark reviewed. Source opens record clicks, not whether the external page loaded. Tracking starts with this update.</p><details><summary>Recently visited opportunities</summary>{activity.recent.length?activity.recent.map(r=><div className="dl-recent" key={r.opportunity.id}><strong>{r.opportunity.title}</strong><span>{r.reviewed_at?"Reviewed":r.viewed_at?"Viewed":"Source opened"}</span><SaveLeadButton id={r.opportunity.id}/><ReviewLeadButton id={r.opportunity.id}/></div>):<p>No recorded activity yet. Open an opportunity brief to get started.</p>}</details><p>Your saved leads, notes and progress stay with your account across devices. Administrators can review this activity.</p><div className="dl-grid"><div>{!leads.length&&<p>Select “Save lead” on an opportunity to keep it here.</p>}{leads.map(l=><button className="dl-lead" key={l.id} onClick={()=>void run(()=>choose(l))}><strong>{l.opportunity.title}</strong><span>{l.stage} · Deadline: {l.opportunity.deadline||"Not listed"}</span></button>)}</div>
 {selected&&<form onSubmit={e=>{e.preventDefault();void run(async()=>{const {stage,notes,factors,next_action,next_action_date}=selected;await request(`/my-leads/${selected.id}`,"PUT",{stage,notes,factors,next_action,next_action_date:next_action_date||null});await reload();setEvents(await request(`/my-leads/${selected.id}/history`));setNotice("Progress saved to your account");});}}><fieldset disabled={busy}><h3>{selected.opportunity.title}</h3><label>Progress<select value={selected.stage} onChange={e=>setSelected({...selected,stage:e.target.value})}>{stages.map(s=><option key={s}>{s}</option>)}</select></label><label>Notes<textarea maxLength={20000} value={selected.notes} onChange={e=>setSelected({...selected,notes:e.target.value})}/></label><label>Next step<input maxLength={2000} value={selected.next_action} onChange={e=>setSelected({...selected,next_action:e.target.value})}/></label><label>Follow-up date<input type="date" value={selected.next_action_date||""} onChange={e=>setSelected({...selected,next_action_date:e.target.value})}/></label><button>Save progress</button></fieldset><ul>{events.map(v=><li key={v.id}>{v.stage} · {v.note} · {v.created_at.slice(0,10)}</li>)}</ul></form>}</div></>}
 {open&&<><details><summary>Change my password</summary><form onSubmit={e=>{e.preventDefault();void run(async()=>{if(password!==confirm)throw new Error("New passwords do not match");await request("/accounts/password","POST",{current_password:current,new_password:password});setCurrent("");setPassword("");setConfirm("");setNotice("Password changed. Other sessions have been signed out.");});}}><fieldset disabled={busy}><label>Current password<input required type="password" autoComplete="current-password" value={current} onChange={e=>setCurrent(e.target.value)}/></label><label>New password (12+ characters)<input required minLength={12} maxLength={200} type="password" autoComplete="new-password" value={password} onChange={e=>setPassword(e.target.value)}/></label><label>Confirm new password<input required type="password" autoComplete="new-password" value={confirm} onChange={e=>setConfirm(e.target.value)}/></label><button>Change password</button></fieldset></form></details>
 {isAdmin&&<><details onToggle={e=>{if(e.currentTarget.open)void run(async()=>setTeam(await request("/my-leads/team/activity")));}}><summary>Team activity — saved leads and progress</summary><button disabled={busy} onClick={()=>void run(async()=>setTeam(await request("/my-leads/team/activity")))}>Refresh activity</button>{team.map(p=><details key={p.email}><summary>{p.name} · {p.email} · {p.leads.length} saved leads{!p.active&&" · Inactive"}</summary><p>{p.activity.viewed} viewed · {p.activity.reviewed} reviewed · {p.activity.source_opened} source links opened</p>{!p.leads.length&&<p>No leads saved yet.</p>}{p.leads.map(l=><article key={l.id}><h4>{l.opportunity.title}</h4><p>{l.stage} · Updated: {l.updated_at.slice(0,10)}</p><p>{l.notes}</p><p>{l.next_action} {l.next_action_date}</p><LeadHistory id={l.id}/></article>)}</details>)}</details><AccountManagement/></>}
 </>}
 </section>;
}
function LeadHistory({id}:{id:number}){const [events,setEvents]=useState<HistoryEvent[]>([]),[error,setError]=useState("");return <details onToggle={e=>{if(e.currentTarget.open)void request(`/my-leads/team/history/${id}`).then(setEvents).catch(e=>setError(e.message));}}><summary>View progress history</summary>{error&&<p role="alert">{error}</p>}<ul>{events.map(e=><li key={e.id}>{e.stage} · {e.note} · {e.created_at.slice(0,10)}</li>)}</ul></details>;}
