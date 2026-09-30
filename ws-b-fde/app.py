"""FDE app — the CALLER (deploy to Workspace B). Config comes entirely from env (see app.yaml).

Eight buttons that reach the target workspace (WS A):

  1. App      · M2M                -> UC connection proxy -> gtm-agent /api/gtm-summary
  2. App      · U2M (forward)      -> reuse this user's WS-B token -> WS A app   (FAILS 401)
  3. Notebook · M2M                -> UC connection proxy -> WS A Jobs run-now
  4. Notebook · U2M (forward)      -> reuse this user's WS-B token -> WS A Jobs   (FAILS 400)
  5. App      · U2M (OAuth)        -> authz-code login to WS A -> app as the user  (works)
  6. Notebook · U2M (OAuth)        -> authz-code login to WS A -> run-now as user  (works)
  7. A2A      · as service principal -> discover Agent Card + message/send via UC connection
  8. A2A      · as the real user     -> discover Agent Card + message/send via U2M OAuth

Buttons 1-4 and 7 use fetch and render inline. 5, 6, 8 need a browser redirect (OAuth),
so they navigate the tab and the callback renders the result with a back link.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from databricks.sdk import WorkspaceClient
import os, json, secrets, hashlib, base64, requests, uuid
from urllib.parse import urlencode

app = FastAPI()

WS_A_HOST   = os.environ.get("WS_A_HOST", "").rstrip("/")          # target workspace (us-west-2)
WS_A_APP    = os.environ.get("WS_A_APP_URL", "").rstrip("/")       # gtm-agent app URL
WS_A_JOB_ID = os.environ.get("WS_A_JOB_ID", "")
CONNECTION_NAME      = os.environ.get("CONNECTION_NAME", "m2m_connection_fde_gtm")
CONNECTION_NAME_JOBS = os.environ.get("CONNECTION_NAME_JOBS", "m2m_connection_fde_jobs_v2")
M2M_CLIENT_ID     = os.environ.get("M2M_CLIENT_ID", "")
M2M_CLIENT_SECRET = os.environ.get("M2M_CLIENT_SECRET", "")
U2M_CLIENT_ID     = os.environ.get("U2M_CLIENT_ID", "")            # the account app-connection client_id
FDE_APP_URL       = os.environ.get("FDE_APP_URL", "").rstrip("/")

# This app's own workspace (WS B); Databricks injects DATABRICKS_HOST WITHOUT scheme.
_b = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
WS_B_HOST = _b if _b.startswith("http") else f"https://{_b}"

PENDING = {}  # state -> {code_verifier, target}


def call_target(target, bearer):
    h = {"Authorization": f"Bearer {bearer}"}
    if target == "app":
        r = requests.get(f"{WS_A_APP}/api/gtm-summary", headers=h, timeout=30)
    else:
        r = requests.post(f"{WS_A_HOST}/api/2.1/jobs/run-now",
                          headers={**h, "Content-Type": "application/json"},
                          json={"job_id": int(WS_A_JOB_ID)}, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return {"http_status": r.status_code, "body": body}


def a2a_call_direct(bearer):
    """A2A client using a given bearer token DIRECTLY against WS A (no UC connection):
    discover the Agent Card, then message/send to the card's advertised url."""
    h = {"Authorization": f"Bearer {bearer}"}
    card_r = requests.get(f"{WS_A_APP}/.well-known/agent.json", headers=h, timeout=30)
    try:
        card = card_r.json()
    except Exception:
        card = card_r.text
    a2a_url = card.get("url") if isinstance(card, dict) else f"{WS_A_APP}/a2a"
    rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
           "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                                   "parts": [{"kind": "text", "text": "Give me my GTM brief"}]}}}
    call_r = requests.post(a2a_url, headers={**h, "Content-Type": "application/json"}, json=rpc, timeout=30)
    try:
        resp = call_r.json()
    except Exception:
        resp = call_r.text
    return {"discovered": {"name": card.get("name") if isinstance(card, dict) else None,
                           "advertised_url": a2a_url,
                           "skills": [s.get("id") for s in card.get("skills", [])] if isinstance(card, dict) else None,
                           "card_http": card_r.status_code},
            "a2a_call_http": call_r.status_code, "agent_response": resp}


def m2m_token():
    r = requests.post(f"{WS_A_HOST}/oidc/v1/token",
                      auth=(M2M_CLIENT_ID, M2M_CLIENT_SECRET),
                      data={"grant_type": "client_credentials", "scope": "all-apis"}, timeout=30)
    return r.json().get("access_token"), r.status_code, r.text


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>WS B -> WS A auth test</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:940px;margin:2rem auto;padding:0 1rem}
 h1{font-size:1.25rem} .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:1rem 0}
 button,a.btn{display:block;padding:13px;border-radius:8px;border:1px solid #ccc;background:#f7f7f9;
   font-size:14px;cursor:pointer;text-align:center;text-decoration:none;color:#111}
 .m2m{border-color:#2a7}.fwd{border-color:#c60}.oauth{border-color:#25c}
 pre{background:#111;color:#0f0;padding:12px;border-radius:8px;white-space:pre-wrap;word-break:break-word;min-height:60px}
 small{color:#666}
</style></head><body>
<h1>Workspace B (FDE) &rarr; Workspace A (us-west-2) &mdash; auth boundary test</h1>
<small>green = M2M &middot; orange = U2M forward (fails) &middot; blue = U2M proper OAuth</small>
<div class=grid>
 <button class=m2m onclick="m2m('app')">1 · App · M2M (connection)</button>
 <button class=fwd onclick="fwd('app')">2 · App · U2M (forward)</button>
 <a class="btn oauth" href="/oauth/start?target=app">5 · App · U2M (OAuth)</a>
 <button class=m2m onclick="m2m('notebook')">3 · Notebook · M2M</button>
 <button class=fwd onclick="fwd('notebook')">4 · Notebook · U2M (forward)</button>
 <a class="btn oauth" href="/oauth/start?target=notebook">6 · Notebook · U2M (OAuth)</a>
 <button onclick="a2a()" style="grid-column:1 / -1;border-color:#7c3aed;background:#f5f3ff">7 · A2A — discover + call as SERVICE PRINCIPAL (via UC connection)</button>
 <a class="btn oauth" href="/oauth/start?target=a2a" style="grid-column:1 / -1;border-color:#7c3aed;background:#faf5ff">8 · A2A — discover + call as the REAL USER (U2M OAuth)</a>
</div>
<div id=banner style="font-size:16px;font-weight:600;margin:10px 0;min-height:22px"></div>
<pre id=out>Click a button…</pre>
<script>
const out=document.getElementById('out'), banner=document.getElementById('banner');
function summarize(d){
 if(d.mode&&d.mode.indexOf('A2A')===0){let txt='';try{txt=d.agent_response.result.artifacts[0].parts.find(p=>p.kind==='text').text;}catch(e){txt='(no artifact)';}
  const disc=d.discovered||{};return '✅ A2A — discovered "'+disc.name+'" (skills: '+(disc.skills||[]).join(', ')+') → '+txt;}
 const r=d.result||{}; const b=(r.body&&typeof r.body==='object')?r.body:{};
 if(b.message) return '✅ WS A responded — served for '+b.served_for+' ('+b.principal_type+') — '+b.message;
 if(b.run_id) return '✅ WS A notebook triggered — run_id '+b.run_id;
 const f=d.forward_attempt_WS_B_token_to_WS_A;
 if(f) return '❌ WS-B token rejected by WS A — HTTP '+f.http_status+' '+(typeof f.body==='string'?f.body:'');
 if(r.http_status) return 'HTTP '+r.http_status;
 return '';
}
function show(d){out.textContent=JSON.stringify(d,null,2); banner.textContent=summarize(d);}
async function m2m(t){banner.textContent='';out.textContent='calling '+t+' via M2M…';
 const r=await fetch('/call/'+t+'/m2m',{method:'POST'});show(await r.json());}
async function fwd(t){banner.textContent='';out.textContent='forwarding this user\\'s WS-B token to WS A for '+t+'…';
 const r=await fetch('/u2m/forward?target='+t);show(await r.json());}
async function a2a(){banner.textContent='';out.textContent='discovering Agent Card + calling via A2A…';
 const r=await fetch('/a2a/demo',{method:'POST'});show(await r.json());}
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE


@app.post("/call/{target}/m2m")
def call_m2m(target: str):
    if target == "app":
        auth = WorkspaceClient().config.authenticate()
        url = f"{WS_B_HOST}/api/2.0/unity-catalog/connections/{CONNECTION_NAME}/proxy/api/gtm-summary"
        r = requests.post(url, headers={**auth, "Content-Type": "application/json"}, timeout=30)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"mode": "M2M", "target": "app",
                "auth": f"UC connection '{CONNECTION_NAME}' proxy -> SP client-credentials",
                "result": {"http_status": r.status_code, "body": body}}
    # notebook: route through a SECOND UC connection whose host is WS A's workspace API,
    # proxying to the Jobs run-now endpoint. Same governance (USE_CONNECTION) as the app path.
    auth = WorkspaceClient().config.authenticate()
    url = f"{WS_B_HOST}/api/2.0/unity-catalog/connections/{CONNECTION_NAME_JOBS}/proxy/api/2.1/jobs/run-now"
    r = requests.post(url, headers={**auth, "Content-Type": "application/json"},
                      json={"job_id": int(WS_A_JOB_ID)}, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return {"mode": "M2M", "target": "notebook",
            "auth": f"UC connection '{CONNECTION_NAME_JOBS}' proxy -> SP client-credentials -> Jobs run-now",
            "result": {"http_status": r.status_code, "body": body}}


@app.post("/a2a/demo")
def a2a_demo():
    """A2A client: discover the GTM agent's Agent Card, then call message/send —
    both hops through the governed M2M UC connection (agent acts as its SP)."""
    auth = WorkspaceClient().config.authenticate()
    base = f"{WS_B_HOST}/api/2.0/unity-catalog/connections/{CONNECTION_NAME}/proxy"

    # 1) DISCOVER: fetch /.well-known/agent.json
    card_r = requests.get(f"{base}/.well-known/agent.json", headers=auth, timeout=30)
    try:
        card = card_r.json()
    except Exception:
        card = card_r.text

    # 2) CALL: JSON-RPC message/send to the agent
    rpc = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
           "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                                   "parts": [{"kind": "text", "text": "Give me my GTM brief"}]}}}
    call_r = requests.post(f"{base}/a2a", headers={**auth, "Content-Type": "application/json"},
                           json=rpc, timeout=30)
    try:
        resp = call_r.json()
    except Exception:
        resp = call_r.text

    return {"mode": "A2A (discover + call via UC connection)",
            "discovered": {
                "name": card.get("name") if isinstance(card, dict) else None,
                "advertised_url": card.get("url") if isinstance(card, dict) else None,
                "skills": [s.get("id") for s in card.get("skills", [])] if isinstance(card, dict) else None,
                "security": list((card.get("securitySchemes") or {}).keys()) if isinstance(card, dict) else None,
                "card_http": card_r.status_code},
            "a2a_call_http": call_r.status_code,
            "agent_response": resp}


@app.get("/u2m/forward")
def u2m_forward(request: Request, target: str = "app"):
    fwd_tok = request.headers.get("X-Forwarded-Access-Token")
    forward = call_target(target, fwd_tok) if fwd_tok else {"note": "no X-Forwarded-Access-Token"}
    return JSONResponse({
        "mode": "U2M (forward)", "target": target,
        "what_happened": "Took THIS user's WS-B token and sent it straight to WS A.",
        "forward_attempt_WS_B_token_to_WS_A": forward,
        "why_it_fails": "Token iss/aud are WS-B, so WS A rejects it. User tokens are workspace-scoped.",
    })


@app.get("/oauth/start")
def oauth_start(target: str = "app"):
    if not U2M_CLIENT_ID:
        return JSONResponse({"error": "U2M_CLIENT_ID not set"})
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    q = urlencode({"response_type": "code", "client_id": U2M_CLIENT_ID,
                   "redirect_uri": f"{FDE_APP_URL}/oauth/callback",
                   "scope": "all-apis offline_access", "state": state,
                   "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(f"{WS_A_HOST}/oidc/v1/authorize?{q}")
    # Carry PKCE verifier + state in a cookie, NOT server memory, so the callback
    # survives app restarts / multiple replicas. SameSite=Lax so it rides the redirect back.
    ctx = base64.urlsafe_b64encode(json.dumps({"v": verifier, "t": target, "s": state}).encode()).decode()
    resp.set_cookie("oauth_ctx", ctx, max_age=600, httponly=True, secure=True, samesite="lax", path="/")
    return resp


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    raw = request.cookies.get("oauth_ctx")
    ctx = None
    if raw:
        try:
            ctx = json.loads(base64.urlsafe_b64decode(raw))
        except Exception:
            ctx = None
    if error or not ctx or ctx.get("s") != state:
        return HTMLResponse(f"<pre>OAuth problem: error={error} cookie_present={bool(raw)} "
                            f"state_match={bool(ctx and ctx.get('s') == state)}</pre><a href='/'>back</a>")
    tr = requests.post(f"{WS_A_HOST}/oidc/v1/token",
                       data={"grant_type": "authorization_code", "code": code,
                             "redirect_uri": f"{FDE_APP_URL}/oauth/callback",
                             "client_id": U2M_CLIENT_ID, "code_verifier": ctx["v"]}, timeout=30)
    user_tok = tr.json().get("access_token")
    target = ctx["t"]

    if target == "a2a":
        a2a = a2a_call_direct(user_tok) if user_tok else {"error": "token_exchange_failed", "detail": tr.text[:300]}
        out = {"mode": "A2A as the real user (U2M OAuth)",
               "logged_in_to_WS_A_as_real_user": bool(user_tok), **a2a}
        try:
            parts = a2a["agent_response"]["result"]["artifacts"][0]["parts"]
            txt = next(p["text"] for p in parts if p.get("kind") == "text")
            data = next(p["data"] for p in parts if p.get("kind") == "data")
            banner = (f"✅ A2A as USER — discovered '{a2a['discovered'].get('name')}' → {txt}  "
                      f"(served_for: {data.get('served_for')} / {data.get('principal_type')})")
        except Exception:
            banner = f"A2A call HTTP {a2a.get('a2a_call_http')}"
        return HTMLResponse(f"<h3>{banner}</h3><pre>{json.dumps(out, indent=2)}</pre><a href='/'>&larr; back</a>")

    proper = call_target(target, user_tok) if user_tok else {"token_exchange_failed": tr.text[:300]}
    out = {"mode": "U2M (proper OAuth via app connection)", "target": target,
           "logged_in_to_WS_A_as_real_user": bool(user_tok), "result": proper}
    body = proper.get("body") if isinstance(proper, dict) else None
    if isinstance(body, dict) and body.get("message"):
        banner = f"✅ WS A responded — served for {body.get('served_for')} ({body.get('principal_type')}) — {body.get('message')}"
    elif isinstance(body, dict) and body.get("run_id"):
        banner = f"✅ WS A notebook triggered as the real user — run_id {body.get('run_id')}"
    else:
        banner = f"HTTP {proper.get('http_status') if isinstance(proper, dict) else '?'}"
    return f"<h3>{banner}</h3><pre>{json.dumps(out, indent=2)}</pre><a href='/'>&larr; back</a>"
