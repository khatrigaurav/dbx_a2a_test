"""GTM agent — the TARGET and A2A SERVER (deploy to Workspace A).

Endpoints:
  GET      /                     health
  GET|POST /api/whoami           echoes the identity the request arrived with
  GET|POST /api/gtm-summary      a personalized "GTM brief" (labels caller user vs SP)
  GET      /.well-known/agent.json   A2A Agent Card (self-configuring from request/env)
  POST     /a2a                  A2A JSON-RPC message/send -> Task + Artifact

Identity comes from the Databricks Apps X-Forwarded-* headers, so the reply is
personalized to WHO called: a real user (U2M) or a service principal (M2M).
No hardcoded URLs — the Agent Card derives its endpoints from the request host and
the injected DATABRICKS_HOST (override with SELF_URL if you need to).
"""
from fastapi import FastAPI, Request
from datetime import datetime, timezone
from databricks.sdk import WorkspaceClient
import base64, json, os, uuid

app = FastAPI()

# Config for the optional `revenue-sum` skill (row-level-security demo). Both are
# required only for that skill; the account-brief skill needs neither.
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", "")   # a SQL warehouse id in WS A
SALES_TABLE  = os.environ.get("SALES_TABLE", "")    # e.g. catalog.schema.sales (has RLS)


def _peek_jwt(token):
    """Best-effort decode of a JWT payload (no verification) for inspection."""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
        return {k: c.get(k) for k in ("sub", "aud", "iss", "scope", "exp")}
    except Exception as e:
        return {"decode_error": str(e)}


def _identity(request: Request):
    name = (request.headers.get("X-Forwarded-Preferred-Username")
            or request.headers.get("X-Forwarded-Email") or "unknown caller")
    tok = request.headers.get("X-Forwarded-Access-Token")
    sub = _peek_jwt(tok).get("sub") if tok else None
    # A user's token subject is an email; a service principal's is a client-id UUID.
    ptype = "user" if (sub and "@" in str(sub)) else "service principal"
    return name, sub, ptype


def _run_revenue(request: Request, acting_tok=None):
    """Sum revenue AS THE CALLER (row-level-security demo).

    For A2A-as-user, the client passes the user's FULL token in the message metadata
    (`actingUserToken`): the Apps ingress only forwards an identity-scoped token (no
    `sql`), so that side-channel token is what lets us query as the user. Otherwise
    (M2M / no user token) we run as the agent app's own service principal. RLS on the
    table then yields a different total per identity.
    """
    if not WAREHOUSE_ID or not SALES_TABLE:
        return {"error": "set WAREHOUSE_ID and SALES_TABLE env vars to enable revenue-sum"}
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if host and not host.startswith("http"):
        host = f"https://{host}"
    if acting_tok:
        # auth_type="pat" forces bearer-token auth so the SDK ignores the app's own
        # injected OAuth creds (DATABRICKS_CLIENT_ID/SECRET) — else it errors on two auth methods.
        sub = _peek_jwt(acting_tok).get("sub")
        w, ran_as = WorkspaceClient(host=host, token=acting_tok, auth_type="pat"), f"{sub} (user · OBO)"
    else:
        sub, w, ran_as = None, WorkspaceClient(), "agent service principal"
    stmt = f"SELECT sum(total_sales) AS total, count(*) AS rows FROM {SALES_TABLE}"
    try:
        r = w.statement_execution.execute_statement(warehouse_id=WAREHOUSE_ID, statement=stmt, wait_timeout="30s")
        state = getattr(r.status.state, "value", str(r.status.state))
        if r.result and r.result.data_array:
            total, rows = r.result.data_array[0]
            return {"ran_as": ran_as, "caller_sub": sub, "state": state,
                    "total_revenue": total, "rows_visible": rows, "table": SALES_TABLE}
        err = r.status.error.message if (r.status and r.status.error) else "no rows"
        return {"ran_as": ran_as, "caller_sub": sub, "state": state, "error": err, "table": SALES_TABLE}
    except Exception as e:
        return {"ran_as": ran_as, "caller_sub": sub, "error": str(e), "table": SALES_TABLE}


@app.get("/")
def root():
    return {"status": "GTM agent (A2A server) is running"}


@app.api_route("/api/whoami", methods=["GET", "POST"])
async def whoami(request: Request):
    fwd = {k: v for k, v in request.headers.items() if k.lower().startswith("x-forwarded")}
    tok = request.headers.get("X-Forwarded-Access-Token")
    result = {
        "message": "GTM agent saw this identity",
        "forwarded_email": request.headers.get("X-Forwarded-Email"),
        "forwarded_token_claims": _peek_jwt(tok) if tok else None,
        "all_x_forwarded_headers": fwd,
    }
    print(result, flush=True)
    return result


@app.api_route("/api/gtm-summary", methods=["GET", "POST"])
async def gtm_summary(request: Request):
    name, sub, ptype = _identity(request)
    result = {
        "agent": "GTM Insights Agent",
        "served_for": name,
        "principal_type": ptype,
        "authenticated_sub": sub,
        "message": f"Hi {name} — your GTM brief: 3 open opps, 2 renewals due this quarter, pipeline +12% WoW.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    print(result, flush=True)
    return result


# --------------------------- A2A ---------------------------
def _agent_card(request: Request):
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    self_url = (os.environ.get("SELF_URL") or f"https://{host}").rstrip("/")
    ws_host = os.environ.get("DATABRICKS_HOST", host).rstrip("/")
    if not ws_host.startswith("http"):
        ws_host = f"https://{ws_host}"
    return {
        "protocolVersion": "0.2.0",
        "name": "GTM Insights Agent",
        "description": "Answers go-to-market questions: account briefs, pipeline, renewals.",
        "url": f"{self_url}/a2a",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {"oauth": {"type": "oauth2", "flows": {
            "clientCredentials": {  # M2M — a service principal acts as itself (buttons 1, 3, 7)
                "tokenUrl": f"{ws_host}/oidc/v1/token",
                "scopes": {"all-apis": "Full workspace API access"}},
            "authorizationCode": {  # U2M — on behalf of a logged-in user (buttons 5, 6, 8)
                "authorizationUrl": f"{ws_host}/oidc/v1/authorize",
                "tokenUrl": f"{ws_host}/oidc/v1/token",
                "scopes": {"all-apis": "Full workspace API access"}},
        }}},
        "security": [{"oauth": ["all-apis"]}],
        "skills": [{
            "id": "account-brief", "name": "Account Brief",
            "description": "Generate a personalized GTM brief for the caller.",
            "tags": ["gtm", "sales"], "examples": ["Give me my GTM brief"],
            "inputModes": ["text/plain"], "outputModes": ["application/json"],
        }, {
            "id": "revenue-sum", "name": "Revenue Sum",
            "description": "Sum of sales revenue VISIBLE TO THE CALLER (row-level security applies).",
            "tags": ["gtm", "revenue", "sql"], "examples": ["Sum of revenue", "total revenue"],
            "inputModes": ["text/plain"], "outputModes": ["application/json"],
        }],
    }


@app.get("/.well-known/agent.json")
def agent_card(request: Request):
    """A2A discovery document — who this agent is, its skills, and how to authenticate."""
    return _agent_card(request)


@app.post("/a2a")
async def a2a(request: Request):
    """Minimal A2A JSON-RPC endpoint: message/send -> a completed Task with an Artifact."""
    body = await request.json()
    rid = body.get("id")
    if body.get("method") != "message/send":
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unsupported method: {body.get('method')}"}}

    parts = body.get("params", {}).get("message", {}).get("parts", [])
    prompt = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text") or "(no text)"
    acting_tok = (body.get("params", {}).get("metadata") or {}).get("actingUserToken")  # A2A-as-user side channel
    name, sub, ptype = _identity(request)
    now = datetime.now(timezone.utc).isoformat()

    if "revenue" in prompt.lower() or "sales" in prompt.lower():
        rev = _run_revenue(request, acting_tok)
        text = (f"Revenue sum from {rev.get('table')}: total={rev.get('total_revenue')} across "
                f"{rev.get('rows_visible')} row(s) — ran as {rev.get('ran_as')}."
                if not rev.get("error") else f"Revenue query error: {rev.get('error')}")
        data, artifact_name = rev, "revenue-sum"
    else:
        text = (f"Hi {name} — GTM brief for '{prompt}': 3 open opps, 2 renewals due this "
                f"quarter, pipeline +12% WoW.")
        data = {"served_for": name, "principal_type": ptype, "received_prompt": prompt, "generated_at": now}
        artifact_name = "gtm-brief"
    print(f"A2A message/send from {name} ({ptype}): {prompt}", flush=True)
    return {"jsonrpc": "2.0", "id": rid, "result": {
        "id": str(uuid.uuid4()), "contextId": str(uuid.uuid4()),
        "status": {"state": "completed", "timestamp": now},
        "artifacts": [{"artifactId": str(uuid.uuid4()), "name": artifact_name, "parts": [
            {"kind": "text", "text": text},
            {"kind": "data", "data": data}]}],
        "kind": "task"}}
