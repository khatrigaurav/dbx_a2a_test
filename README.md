# dbx_a2a_test — cross-workspace auth & A2A on Databricks Apps

A small, deployable demo of **how one Databricks App calls another across workspaces** under
every relevant auth model, and then layers **A2A (Agent-to-Agent)** on top.

You deploy two apps: a **GTM agent** (the target / A2A server) in **Workspace A**, and an **FDE app**
(the caller / A2A client) in **Workspace B**. The FDE app has **10 buttons**:

| # | Button | Mechanism | Expected |
|---|---|---|---|
| 1 | App · M2M | service principal via **UC connection** → `/api/gtm-summary` | ✅ served for the **SP** |
| 2 | App · U2M (forward) | reuse the caller's WS-B user token against WS A | ❌ **401** (by design) |
| 3 | Notebook · M2M | SP via **UC connection** → Jobs `run-now` | ✅ run triggered |
| 4 | Notebook · U2M (forward) | reuse WS-B user token against WS A Jobs API | ❌ **400 Invalid Token** (by design) |
| 5 | App · U2M (OAuth) | user logs in **to WS A** (authz-code + PKCE) → app | ✅ served for the **real user** |
| 6 | Notebook · U2M (OAuth) | user logs in to WS A → Jobs `run-now` | ✅ run as the user |
| 7 | A2A · as SP | discover Agent Card + `message/send` via UC connection | ✅ agent serves the **SP** |
| 8 | A2A · as user | discover Agent Card + `message/send` via U2M OAuth | ✅ agent serves the **real user** |
| 9 | A2A `revenue-sum` · as SP | agent runs `SELECT sum(...)` as its **own SP** | ✅ sees **all** rows (RLS) |
| 10 | A2A `revenue-sum` · as user | agent runs the query **as you** via `actingUserToken` | ✅ sees **only your** rows (RLS) |

Buttons 9–10 are an optional **row-level-security** demo (see Step 2b); they need `WAREHOUSE_ID` +
`SALES_TABLE` set on the GTM agent. The other 8 run without them.

**The lesson:** M2M crosses workspaces cleanly; a **forwarded user token does not** (it's workspace-scoped);
**proper U2M** works only if the user authenticates *to the target* (which needs an account-level OAuth app).
A2A's protocol is identity-agnostic — the auth you present decides *who the agent serves*.

```
Workspace B (caller)                              Workspace A (target)
┌────────────────────────────┐                   ┌────────────────────────────┐
│ FDE app (8 buttons)         │  ── M2M (UC conn) ►│ GTM agent app              │
│  UC conn → app  ───────────┼───────────────────►│  /api/gtm-summary          │
│  UC conn → Jobs ───────────┼── M2M (UC conn) ──►│  Jobs run-now              │
│  OAuth (PKCE) ─────────────┼── U2M redirect ───►│  /.well-known/agent.json   │  A2A card
└────────────────────────────┘  forward → 401     │  /a2a  (JSON-RPC)          │  A2A server
      app service principal                        └────────────────────────────┘
                     OAuth app connection (client_id) registered in WS A's ACCOUNT
```

See `oauth-flow-diagram.html` (open in any browser, fully offline) for the U2M OAuth sequence.

---

## Prerequisites

- **Databricks CLI** ≥ 0.230 (`databricks -v`).
- **Two Databricks workspaces**, both attached to the **same Unity Catalog metastore** (so the caller can
  create UC connections and reach the target). They can be the same account.
- The **target** workspace must be in an **Apps-supported region** (e.g. `us-west-2`; **`us-west-1` is NOT supported**).
- **Account admin** on the **target workspace's account** — required only for the U2M OAuth app connection (buttons 5, 6, 8). M2M and the "forward" demos don't need it.

Throughout, `WS_A` = target (GTM agent), `WS_B` = caller (FDE app). Set two CLI profiles:

```bash
databricks auth login --host https://<WS_A>.cloud.databricks.com --profile wsa   # target
databricks auth login --host https://<WS_B>.cloud.databricks.com --profile wsb   # caller
```

---

## Step 1 — Deploy the GTM agent (target, WS A)

```bash
git clone https://github.com/khatrigaurav/dbx_a2a_test && cd dbx_a2a_test
ME=$(databricks current-user me -p wsa | python3 -c "import sys,json;print(json.load(sys.stdin)['userName'])")

# create + deploy the app (no config needed — it self-configures its Agent Card)
databricks apps create gtm-agent -p wsa
databricks sync ws-a-gtm "/Workspace/Users/$ME/dbx_a2a_gtm" -p wsa
databricks apps deploy gtm-agent --source-code-path "/Workspace/Users/$ME/dbx_a2a_gtm" -p wsa

# note the app URL it prints, e.g. https://gtm-agent-xxxx.aws.databricksapps.com
databricks apps get gtm-agent -p wsa | python3 -c "import sys,json;print(json.load(sys.stdin)['url'])"
```

Create the notebook job (target for buttons 3, 4, 6):

```bash
databricks workspace import "/Workspace/Users/$ME/dbx_a2a_nb" \
  --file ws-a-gtm/notebook/xws_notebook.py --language PYTHON --format SOURCE --overwrite -p wsa
databricks jobs create --json '{"name":"dbx_a2a_nb","tasks":[{"task_key":"run_nb","notebook_task":{"notebook_path":"/Workspace/Users/'"$ME"'/dbx_a2a_nb"}}]}' -p wsa
# note the returned job_id
```

## Step 2 — Create the M2M service principal (WS A)

```bash
# 1) create the SP
SP=$(databricks service-principals create --display-name dbx-a2a-m2m -p wsa)
SPID=$(echo "$SP" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
CID=$(echo "$SP" | python3 -c "import sys,json;print(json.load(sys.stdin)['applicationId'])")
echo "client_id=$CID"

# 2) OAuth secret (store it; you'll paste it into ws-b-fde/app.yaml)
databricks service-principal-secrets-proxy create "$SPID" -p wsa

# 3) entitlement — REQUIRED so the SP identity resolves and it can call the Jobs API
databricks service-principals patch "$SPID" --json '{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"entitlements","value":[{"value":"workspace-access"}]}]}' -p wsa

# 4) grants: CAN_USE on the app, CAN_MANAGE_RUN on the job
databricks apps update-permissions gtm-agent --json '{"access_control_list":[{"service_principal_name":"'"$CID"'","permission_level":"CAN_USE"}]}' -p wsa
databricks permissions update jobs <JOB_ID> --json '{"access_control_list":[{"service_principal_name":"'"$CID"'","permission_level":"CAN_MANAGE_RUN"}]}' -p wsa
```

## Step 2b — (optional) Row-level-security table for the `revenue-sum` skill (buttons 9, 10)

Skip this if you don't want the RLS demo. It shows the same A2A skill returning a **different total**
depending on whether the agent runs the query as its **own SP** (button 9, all rows) or **as you**
(button 10, only your rows). Run in a WS A SQL editor / notebook:

```sql
CREATE TABLE IF NOT EXISTS <catalog>.<schema>.sales (region STRING, owner STRING, total_sales DOUBLE);
INSERT INTO <catalog>.<schema>.sales VALUES
  ('NA','you@example.com',10000),('EMEA','you@example.com',15000),
  ('APAC','someone@example.com',30000),('LATAM','someone@example.com',20000),('ANZ','someone@example.com',25000);

-- row-level security: a user sees only rows they own; the agent SP sees all
ALTER TABLE <catalog>.<schema>.sales SET ROW FILTER <catalog>.<schema>.sales_rls ON (owner);
```

Define the filter so your SP sees everything and users see only their rows (adapt to your governance),
then grant read access to **both** the end user and the **GTM agent app's SP** (its client_id is printed by
`databricks apps get gtm-agent -p wsa`):

```bash
AGENT_SP='<gtm-agent app SP client_id>'
for P in "you@example.com" "$AGENT_SP"; do
  databricks grants update catalog  <catalog> --json '{"changes":[{"principal":"'"$P"'","add":["USE_CATALOG"]}]}' -p wsa
  databricks grants update schema   <catalog>.<schema> --json '{"changes":[{"principal":"'"$P"'","add":["USE_SCHEMA"]}]}' -p wsa
  databricks grants update table    <catalog>.<schema>.sales --json '{"changes":[{"principal":"'"$P"'","add":["SELECT"]}]}' -p wsa
done
# the agent SP also needs SQL access + CAN_USE on the warehouse
databricks service-principals patch <AGENT_SP_scim_id> --json '{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"entitlements","value":[{"value":"databricks-sql-access"}]}]}' -p wsa
databricks permissions update warehouses <WAREHOUSE_ID> --json '{"access_control_list":[{"service_principal_name":"'"$AGENT_SP"'","permission_level":"CAN_USE"}]}' -p wsa
```

Then set `WAREHOUSE_ID` and `SALES_TABLE` in `ws-a-gtm/app.yaml` before deploying the agent (Step 1).

## Step 3 — Create the two UC connections (in the caller's metastore, WS B)

Both are `HTTP` connections with `OAuth M2M`, using the SP client_id/secret from Step 2.
One points at the **GTM app host**, the other at the **WS A workspace host** (for the Jobs API).

```bash
SECRET='<paste M2M SP secret>'
# connection A: -> GTM app
databricks connections create -p wsb --json '{
  "name":"m2m_connection_fde_gtm","connection_type":"HTTP",
  "options":{"host":"https://<gtm-agent-xxxx>.aws.databricksapps.com","port":"443","base_path":"/",
    "client_id":"'"$CID"'","client_secret":"'"$SECRET"'","oauth_scope":"all-apis",
    "token_endpoint":"https://<WS_A>.cloud.databricks.com/oidc/v1/token"}}'
# connection B: -> WS A workspace (Jobs API)
databricks connections create -p wsb --json '{
  "name":"m2m_connection_fde_jobs","connection_type":"HTTP",
  "options":{"host":"https://<WS_A>.cloud.databricks.com","port":"443","base_path":"/",
    "client_id":"'"$CID"'","client_secret":"'"$SECRET"'","oauth_scope":"all-apis",
    "token_endpoint":"https://<WS_A>.cloud.databricks.com/oidc/v1/token"}}'
```

(You'll grant the FDE app's SP `USE_CONNECTION` in Step 5, after the app exists.)

## Step 4 — Create the FDE app (caller, WS B)

```bash
databricks apps create fde-app -p wsb
databricks apps get fde-app -p wsb | python3 -c "import sys,json;print('FDE URL:',json.load(sys.stdin)['url'])"
databricks apps get fde-app -p wsb | python3 -c "import sys,json;print('FDE app SP:',json.load(sys.stdin)['service_principal_client_id'])"
```

## Step 5 — Grant the FDE app's SP `USE_CONNECTION` (WS B)

```bash
FDE_SP='<FDE app SP client_id from Step 4>'
for CONN in m2m_connection_fde_gtm m2m_connection_fde_jobs; do
  databricks grants update connection "$CONN" --json '{"changes":[{"principal":"'"$FDE_SP"'","add":["USE_CONNECTION"]}]}' -p wsb
done
```

## Step 6 — Register the account-level OAuth app connection (for U2M — buttons 5, 6, 8)

This is the one step that needs **account admin on the target's account**. It creates the OAuth client that
lets a user log **in to WS A** and mint a WS-A token. The FDE app's callback must be registered as a redirect URL.

**Option A — Account console (UI):**
1. Go to `https://accounts.cloud.databricks.com` and sign in as an **account admin of WS A's account**.
2. **Settings → App connections → Add connection** (choose a *custom* app integration).
3. Fill in:
   - **Name:** `dbx-a2a-u2m`
   - **Redirect URLs:** `https://<this-fde-app-xxxx>.aws.databricksapps.com/oauth/callback` (from Step 4 — must match **exactly**)
   - **Scopes:** `all-apis`, `offline_access`
   - **Generate a client secret:** **OFF** (public / PKCE client — this app uses PKCE)
4. **Add**, then copy the **Client ID**.

**Option B — CLI** (account-admin profile for WS A's account):

```bash
databricks auth login --host https://accounts.cloud.databricks.com --account-id <WS_A_ACCOUNT_ID> --profile acct
databricks account custom-app-integration create -p acct --json '{
  "name":"dbx-a2a-u2m",
  "redirect_urls":["https://<this-fde-app-xxxx>.aws.databricksapps.com/oauth/callback"],
  "scopes":["all-apis","offline_access"],
  "confidential":false
}'
# copy the returned client_id
```

> **Why account-level?** An OAuth client that can mint *user* tokens is a trust anchor for the whole account,
> so only account admins can register one — and it must live in the **same account as the target workspace**
> (a client from a different account is rejected by WS A's authorize endpoint). The `redirect_urls` are a
> security whitelist: WS A will only deliver the login `code` to a pre-registered URL.

## Step 7 — Configure & deploy the FDE app (WS B)

```bash
cp ws-b-fde/app.yaml.example ws-b-fde/app.yaml
# edit ws-b-fde/app.yaml and fill: WS_A_HOST, WS_A_APP_URL, WS_A_JOB_ID,
#   M2M_CLIENT_ID, M2M_CLIENT_SECRET, U2M_CLIENT_ID (from Step 6), FDE_APP_URL

databricks sync ws-b-fde "/Workspace/Users/$ME/dbx_a2a_fde" -p wsb
databricks apps deploy fde-app --source-code-path "/Workspace/Users/$ME/dbx_a2a_fde" -p wsb
```

## Step 8 — Test

Open the FDE app URL in a browser and click the buttons.
- **1, 3, 7** (M2M / A2A-as-SP) → succeed, agent serves the **service principal**.
- **2, 4** (forward) → fail with `401` / `400 Invalid Token` — this is the point.
- **5, 6, 8** (U2M OAuth) → you're redirected to WS A to log in/consent, then it succeeds **as you**.
- **9, 10** (revenue-sum, if Step 2b done) → **9** returns the full total (agent's SP sees all rows);
  **10** logs you in and returns only **your** total (RLS applied to the real user).

---

## How it works (concepts)

- **M2M** — a service principal gets a WS-A token via client-credentials; wrapped in a **UC HTTP connection**
  for governance (`USE_CONNECTION` + audit). Crosses workspaces. Target sees the SP.
- **U2M — forward** — reusing the caller's WS-B token fails: its `iss`/`aud` are WS-B, so WS A rejects it.
  User tokens are workspace-scoped; you cannot "forward a user" across workspaces.
- **U2M — proper OAuth** — the user logs in *to WS A* (authz-code + PKCE), so WS A mints the token and sees the
  real user. Needs the account-level OAuth app connection (Step 6). PKCE `state`/`code_verifier` are stored in a
  cookie (not server memory) so the flow survives app restarts / multiple replicas.
- **A2A** — the GTM agent publishes an **Agent Card** at `/.well-known/agent.json` (name, skills, `securitySchemes`,
  and its `url`) and serves **JSON-RPC `message/send`** at `/a2a`, returning a **Task** with an **Artifact**.
  The client discovers the card then calls it — the *protocol* is identical whether it authenticates as the SP
  (button 7) or the user (button 8); only the presented auth changes who the agent serves.
- **A2A acting as the user (`actingUserToken`)** — a second-hop app that must run a **scoped** API (e.g. `sql`)
  **as the user** can't rely on the ingress-forwarded `X-Forwarded-Access-Token`: that token is **identity-scoped
  only** (good enough to see *who* called, not to run `sql`). So button 10's client passes the user's **full**
  OAuth token in the A2A message `metadata.actingUserToken`, and the agent uses it with `auth_type="pat"` to query
  as the user. Button 9 passes no such token, so the agent runs as its own SP — and RLS yields the different totals.

## Gotchas (learned the hard way)

1. **Apps aren't in every region** — `us-west-1` is unsupported; use e.g. `us-west-2`.
2. **App must bind `$DATABRICKS_APP_PORT`** (often 8000), not a hardcoded port — else a silent 502. Both `app.yaml`s do this.
3. **`DATABRICKS_HOST` is injected without a scheme** — the code prepends `https://`.
4. **A SCIM-created SP needs the `workspace-access` entitlement** (Step 2.3) — else its identity won't resolve and Jobs API returns 403.
5. **The OAuth app connection must be in the TARGET's account**, needs account admin, and its **redirect URL must match exactly**.
6. **A UC connection never forwards the inbound user token** — it injects its own stored credential.

## Layout

```
ws-a-gtm/     GTM agent (target / A2A server): app.py, app.yaml, requirements.txt, notebook/
ws-b-fde/     FDE app (caller / A2A client): app.py, app.yaml.example, requirements.txt
oauth-flow-diagram.html   offline diagram of the U2M OAuth sequence
```
