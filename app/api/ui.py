INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ProofOps</title>
  <link rel="stylesheet" href="/ui/style.css">
</head>
<body>
  <main class="shell">
    <header><div><h1>ProofOps</h1><p>Verified renewal outreach agent</p></div><span id="sessionState">Signed out</span></header>
    <section id="loginCard" class="card">
      <h2>Demo sign in</h2>
      <form id="loginForm"><input id="password" type="password" autocomplete="current-password" placeholder="Demo password" required><button>Sign in</button></form>
      <p class="muted">Session actor is assigned by the server; the browser cannot choose it.</p>
    </section>
    <section id="appCard" class="card hidden">
      <h2>Start verified outreach</h2>
      <form id="goalForm">
        <label>Goal<textarea id="goal" required>Make sure ACME's renewal outreach is handled without contacting them unnecessarily.</textarea></label>
        <label>Company<input id="company" value="ACME" required></label>
        <label>Deadline<input id="deadline" type="datetime-local"></label>
        <button>Start</button>
      </form>
    </section>
    <section id="runCard" class="card hidden">
      <div class="row"><h2>Run <span id="runId"></span></h2><button id="refreshBtn" class="secondary">Refresh</button></div>
      <div id="statusPill" class="pill"></div>
      <div id="proposal" class="proposal hidden"><h3>Proposed action</h3><dl><dt>To</dt><dd id="target"></dd><dt>Subject</dt><dd id="subject"></dd><dt>Body</dt><dd><pre id="body"></pre></dd></dl></div>
      <div class="actions"><button id="approveBtn" class="hidden">Approve exact action</button><button id="executeBtn" class="hidden">Execute approved action</button><button id="resumeBtn" class="secondary hidden">Resume / reconcile</button><button id="rejectBtn" class="danger hidden">Reject</button></div>
      <h3>Evidence & audit timeline</h3><div id="timeline" class="timeline"></div>
    </section>
    <section id="errorCard" class="card error hidden"><strong>Request failed</strong><pre id="errorText"></pre></section>
  </main>
  <script src="/ui/app.js" defer></script>
</body>
</html>'''

STYLE_CSS = r'''
:root { font-family: Inter, ui-sans-serif, system-ui, sans-serif; color-scheme: light; }
body { margin:0; background:#f6f7f9; color:#111827; }
.shell { max-width:900px; margin:32px auto; padding:0 20px 60px; }
header,.row { display:flex; align-items:center; justify-content:space-between; gap:16px; }
h1 { margin:0; font-size:32px; } header p{margin:4px 0;color:#6b7280}
.card { background:white; border:1px solid #e5e7eb; border-radius:16px; padding:20px; margin-top:18px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.hidden { display:none !important; }
form { display:grid; gap:12px; } label{display:grid;gap:6px;font-weight:600} input,textarea{font:inherit;padding:10px;border:1px solid #d1d5db;border-radius:10px} textarea{min-height:84px}
button { border:0; background:#111827; color:white; padding:10px 14px; border-radius:10px; font-weight:700; cursor:pointer; }
button.secondary{background:#e5e7eb;color:#111827}.danger{background:#991b1b}.actions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.muted{color:#6b7280;font-size:14px}.pill{display:inline-block;padding:6px 10px;border-radius:999px;background:#eef2ff;color:#3730a3;font-weight:700}.proposal{margin-top:16px;border-left:4px solid #4f46e5;padding-left:14px}dl{display:grid;grid-template-columns:90px 1fr;gap:8px}dt{font-weight:700}dd{margin:0}pre{white-space:pre-wrap;word-break:break-word}.timeline{display:grid;gap:8px}.event{border-left:3px solid #d1d5db;padding:8px 12px;background:#f9fafb;border-radius:0 8px 8px 0}.event strong{display:block}.event small{color:#6b7280}.error{border-color:#fecaca;color:#991b1b}
'''

APP_JS = r'''
let csrf = sessionStorage.getItem('proofops_csrf') || '';
let runId = sessionStorage.getItem('proofops_run_id') || '';
let actionId = null;
let approvalId = null;
const $ = (id) => document.getElementById(id);
function show(id,on=true){$(id).classList.toggle('hidden',!on)}
function idem(){return crypto.randomUUID()}
function err(e){show('errorCard',true);$('errorText').textContent=typeof e==='string'?e:JSON.stringify(e,null,2)}
async function api(path, opts={}){
  const headers={'Content-Type':'application/json',...(opts.headers||{})};
  if(opts.mutate){headers['X-CSRF-Token']=csrf;headers['Idempotency-Key']=opts.idem||idem()}
  const r=await fetch(path,{method:opts.method||'GET',credentials:'same-origin',headers,body:opts.body?JSON.stringify(opts.body):undefined});
  const data=await r.json().catch(()=>({code:'INVALID_RESPONSE',message:r.statusText}));
  if(!r.ok) throw data;
  return data;
}
async function login(ev){ev.preventDefault();try{const d=await api('/api/session',{method:'POST',body:{password:$('password').value}});csrf=d.csrf_token;sessionStorage.setItem('proofops_csrf',csrf);$('sessionState').textContent=`Signed in as ${d.actor}`;show('loginCard',false);show('appCard',true);if(runId){show('runCard',true);await refresh();}}catch(e){err(e)}}
async function start(ev){ev.preventDefault();try{const deadline=$('deadline').value?new Date($('deadline').value).toISOString():null;const d=await api('/api/runs',{method:'POST',mutate:true,body:{goal_text:$('goal').value,company:$('company').value,deadline}});runId=String(d.run_id);sessionStorage.setItem('proofops_run_id',runId);show('runCard',true);await refresh();}catch(e){err(e)}}
async function refresh(){if(!runId)return;try{const d=await api(`/api/runs/${runId}`);render(d);}catch(e){err(e)}}
function render(d){$('runId').textContent=d.run.id;$('statusPill').textContent=d.run.state;actionId=d.actions.length?d.actions[d.actions.length-1].id:null;approvalId=d.approvals.length?d.approvals[d.approvals.length-1].id:null;const a=d.actions.length?d.actions[d.actions.length-1]:null;if(a){show('proposal',true);$('target').textContent=a.target||'';$('subject').textContent=a.payload?.subject||'';$('body').textContent=a.payload?.body_text||'';}else show('proposal',false);show('approveBtn',!!a&&a.state==='APPROVAL_REQUIRED');show('executeBtn',!!a&&a.state==='APPROVED'&&!!approvalId);show('rejectBtn',!!a&&['APPROVAL_REQUIRED','APPROVED'].includes(a.state)&&!!approvalId);show('resumeBtn',['PLANNING','EXECUTING','UNKNOWN','RECONCILING'].includes(d.run.state));$('timeline').innerHTML='';for(const x of d.timeline){const el=document.createElement('div');el.className='event';const strong=document.createElement('strong');strong.textContent=x.label;const small=document.createElement('small');small.textContent=x.at;const p=document.createElement('div');p.textContent=x.detail||'';el.append(strong,small,p);$('timeline').appendChild(el)}}
async function approve(){try{const d=await api(`/api/runs/${runId}/approve`,{method:'POST',mutate:true,body:{action_id:actionId,ttl_seconds:600}});approvalId=d.approval_id;await refresh();}catch(e){err(e)}}
async function execute(){try{await api(`/api/runs/${runId}/execute`,{method:'POST',mutate:true,body:{action_id:actionId,approval_id:approvalId}});await refresh();}catch(e){err(e)}}
async function resume(){try{await api(`/api/runs/${runId}/resume`,{method:'POST',mutate:true,body:{}});await refresh();}catch(e){err(e)}}
async function reject(){try{await api(`/api/runs/${runId}/reject`,{method:'POST',mutate:true,body:{action_id:actionId,approval_id:approvalId}});await refresh();}catch(e){err(e)}}
$('loginForm').addEventListener('submit',login);$('goalForm').addEventListener('submit',start);$('refreshBtn').onclick=refresh;$('approveBtn').onclick=approve;$('executeBtn').onclick=execute;$('resumeBtn').onclick=resume;$('rejectBtn').onclick=reject;
'''
