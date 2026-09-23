import { useState } from "react";
import "./login.css";
export function LoginScreen({onSuccess}:{onSuccess:()=>void}) {
 const [token]=useState(()=>new URLSearchParams(location.hash.slice(1)).get("setup")||"");
 const [mode,setMode]=useState<"login"|"register"|"forgot">("login");
 const [name,setName]=useState(""),[email,setEmail]=useState(""),[password,setPassword]=useState(""),[confirmation,setConfirmation]=useState(""),[show,setShow]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(""),[message,setMessage]=useState("");
 function changeMode(next:typeof mode){setMode(next);setError("");setMessage("");setPassword("");setConfirmation("");}
 async function submit(e:React.FormEvent){
  e.preventDefault();setError("");setMessage("");
  if((token||mode==="register")&&password!==confirmation){setError("Passwords do not match");return;}
  setBusy(true);
  try {
   const path=token?"/activate":mode==="register"?"/register":mode==="forgot"?"/forgot-password":"";
   const payload=token?{token,password}:mode==="register"?{email,name,password}:mode==="forgot"?{email}:{email,password};
   const r=await fetch("/api/login"+path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
   // Read as text first. r.json() throws "Unexpected end of JSON input" on an
   // empty body, and the user sees that instead of what actually happened —
   // the backend being down, or the endpoint not existing.
   const raw=await r.text();
   let data:any={};
   try{data=raw?JSON.parse(raw):{};}catch{/* a proxy error page, not JSON */}
   if(!raw)throw new Error("The server did not respond. Is the backend running on http://localhost:8000?");
   if(r.status===404)throw new Error("This backend has no sign-in endpoint yet (POST /api/login"+path+" is missing).");
   if(!r.ok)throw new Error(typeof data.detail==="string"?data.detail:`Sign-in failed — HTTP ${r.status}`);
   if(!token&&mode==="forgot"){setMessage(data.message);return;}
   history.replaceState({},"","?view=user");setPassword("");onSuccess();
  }catch(e){setError(e instanceof Error?e.message:"Server unavailable");}finally{setBusy(false);}
 }
 return <main className="login-page">
  <section className="login-story"><strong className="login-brand">CMS <small>LEAD SCANNING PLATFORM</small></strong><div><small>FROM DISCOVERY TO IMPACT</small><h1>Your next opportunity.<br/>Your own journey.</h1><p>Discover relevant funding, build stronger applications and save leads and keep your progress in your personal dashboard.</p><div className="login-preview">YOUR OPPORTUNITY JOURNEY<h3>Discover → Apply → Grow</h3><p>Discover · Save · Track</p></div></div><small>Funding & opportunity intelligence</small></section>
  <section className="login-form-side"><form onSubmit={submit}>
   <small>WELCOME TO YOUR DASHBOARD</small>
   <h2>{token?"Set your password":mode==="register"?"Create your account":mode==="forgot"?"Forgot password?":"Welcome back"}</h2>
   {token&&<p>Choose your personal password to continue.</p>}
   {!token&&mode==="register"&&<p>Choose your password to create your account immediately. Your account starts with user access.</p>}
   {!token&&mode==="forgot"&&<p>Enter your account email and we’ll send you a password reset link.</p>}
   {!token&&mode==="register"&&<label>Your name<input required autoComplete="name" maxLength={200} value={name} onChange={e=>setName(e.target.value)}/></label>}
   {!token&&<label>Work email<input type="email" required autoComplete="username" maxLength={320} value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@organisation.org"/></label>}
   {(token||mode!=="forgot")&&<label>{token?"Choose password":"Your password"}<div className="login-password"><input required type={show?"text":"password"} minLength={token||mode==="register"?12:1} maxLength={200} autoComplete={token||mode==="register"?"new-password":"current-password"} value={password} onChange={e=>setPassword(e.target.value)}/><button type="button" aria-label={show?"Hide password":"Show password"} onClick={()=>setShow(!show)}>{show?"Hide":"Show"}</button></div></label>}
   {(token||mode==="register")&&<><small>At least 12 characters.</small><label>Confirm password<input type="password" required autoComplete="new-password" value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label></>}
   {!token&&mode==="login"&&<button type="button" className="login-text-button" disabled={busy} onClick={()=>changeMode("forgot")}>Forgot password?</button>}
   <p role="alert" className="login-error">{error}</p>
   {message&&<p role="status" className="login-success">{message}</p>}
   <button className="login-submit" disabled={busy}>{busy?"Please wait…":token?"Save password & continue":mode==="register"?"Register":mode==="forgot"?"Send reset link":"Sign in"}</button>
   {!token&&<div className="login-help">{mode==="login"?<>New here? <button type="button" disabled={busy} className="login-text-button" onClick={()=>changeMode("register")}>Register</button></>:<button type="button" disabled={busy} className="login-text-button" onClick={()=>changeMode("login")}>Already have an account? Sign in</button>}</div>}
   <footer>Admin access is granted by your administrator.</footer>
  </form></section>
 </main>;
}
