# MCP Approval Demo Runbook

This runbook proves the Mobile Approval Console / Aixion Control Tower MCP wait-mode path end to end.

The goal is not to add another feature. The goal is to prove the product promise:

```text
MCP client sends mutating tool call
-> gateway creates approval
-> MCP Queue shows pending request
-> operator opens linked approval
-> operator approves
-> gateway resolves
-> downstream MCP server receives exactly one call
-> MCP Queue refreshes to FORWARDED
-> audit trail proves the path
```

## What this validates

1. A mutating MCP request is not forwarded immediately.
2. The gateway creates an approval request.
3. The MCP pending queue exposes the request as `WAITING_FOR_APPROVAL`.
4. The same approval decision API used by Android marks the approval `APPROVED` and freezes `approved_payload_hash`.
5. The gateway resolve endpoint forwards the approved request exactly once.
6. The pending queue refreshes to `FORWARDED`.
7. A second resolve does not duplicate the downstream MCP call.
8. Audit contains:
   - `mcp.approval_requested`
   - `approval.decision`
   - `FORWARDED_AFTER_APPROVAL`

## Fast backend validation

From the repository root:

```bash
cd backend
python scripts/validate_mcp_approval_demo.py
```

Expected ending:

```text
MCP approval demo validation PASSED
```

The script uses:

```text
AIXION_AUTH_ENABLED=false
AIXION_DB_PATH=backend/runtime/mcp_approval_demo_validation.sqlite3
```

It resets only its own demo validation store path. It does not require a running uvicorn server because it uses FastAPI `TestClient` against the app directly.

## Backend pytest sanity

Run the MCP gateway route tests:

```bash
cd backend
python -m pytest tests/test_mcp_gateway_routes.py
```

Run the full backend suite:

```bash
cd backend
python -m pytest
```

Do not claim CI is green unless GitHub Actions confirms it.

## Choose demo mode

There are now two valid demo modes.

### Fast local demo mode

Use this when you want the fastest MCP proof and do not care about login/session behavior:

```bash
AIXION_AUTH_ENABLED=false
```

This is the default used by `backend/scripts/run_demo_server.sh`.

### Authenticated Android MVP mode

Use this when validating the real Android Account screen, saved bearer token, and protected API calls:

```bash
AIXION_AUTH_ENABLED=true
```

In authenticated mode, open the Android `Acct` screen first, register or login, then use Home, Review, MCP Queue, Audit, and other protected screens.

Hard rule: if auth is enabled and Android is not logged in, protected backend calls should fail. That is correct behavior.

## Start the demo backend

For local emulator-only demo with auth disabled:

```bash
cd backend
AIXION_AUTH_ENABLED=false uvicorn app.main:app --reload
```

For a physical Android phone demo, localhost binding is not enough. Start the backend on all interfaces:

```bash
cd backend
bash scripts/run_demo_server.sh
```

Equivalent manual command with auth disabled:

```bash
cd backend
AIXION_AUTH_ENABLED=false uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Equivalent manual command with auth enabled:

```bash
cd backend
AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Hard rule: a real phone must use your computer LAN IP, not `127.0.0.1` and not `10.0.2.2`.

## Authenticated Android validation

Use this after PRs that touch Android auth, API clients, approval actions, or MCP Queue.

1. Start backend with auth enabled:

```bash
cd backend
AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

2. Build Android with a reachable backend URL.

For emulator:

```bash
cd mobile/android
./gradlew assembleDebug
```

For physical phone:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

3. Open Android `Acct`.
4. Register with email, password, and display name, or login with an existing user.
5. Confirm the screen shows an active session.
6. Open Home, Review, MCP Queue, Audit, and Acct.
7. Confirm protected requests work after login.
8. Clear saved session from `Acct`.
9. Confirm protected requests fail until login/register again.

## Manual API validation path

The commands below assume auth is disabled and the backend is reachable at `http://127.0.0.1:8000` from your terminal.

If auth is enabled, first register/login and pass the bearer token:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"change-this-demo-password","display_name":"Demo Operator"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

Then add this header to protected `curl` calls:

```bash
-H "Authorization: Bearer $TOKEN"
```

### 1. Create a project

```bash
curl -s -X POST http://127.0.0.1:8000/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "MCP approval live demo",
    "description": "Live wait-mode demo",
    "mode": "STRICT",
    "rules": ["Mutating MCP tools require approval"]
  }'
```

Copy the returned `id` as `PROJECT_ID`.

### 2. Submit a mutating MCP call

```bash
curl -s -X POST http://127.0.0.1:8000/mcp-gateway/requests \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "PROJECT_ID",
    "request": {
      "server_name": "child-test-server",
      "tool_name": "update_config",
      "arguments": {"key": "demo", "value": "approved-value"},
      "session_id": "manual-demo-session",
      "requested_by": "manual-demo-client",
      "mutating": true
    }
  }'
```

Expected response:

```json
{
  "forwarded": false,
  "approval_required": true,
  "approval_request_id": "approval_...",
  "status": "REQUESTED"
}
```

Copy `approval_request_id` as `APPROVAL_ID`.

### 3. Confirm MCP Queue pending state

```bash
curl -s "http://127.0.0.1:8000/mcp-gateway/pending-requests?approval_request_id=APPROVAL_ID"
```

Expected status:

```text
WAITING_FOR_APPROVAL
```

On Android, this is the point where the MCP Queue screen should show the pending request and allow opening the linked approval.

### 4. Approve from the approval decision path

```bash
curl -s -X POST http://127.0.0.1:8000/approvals/APPROVAL_ID/decision \
  -H 'Content-Type: application/json' \
  -d '{"decision": "approve", "reason": "Manual MCP demo approval"}'
```

Expected status:

```text
APPROVED
```

Also verify `approved_payload_hash` is present. Without this hash, the gateway must not forward.

### 5. Resolve the MCP gateway

```bash
curl -s -X POST http://127.0.0.1:8000/mcp-gateway/approvals/APPROVAL_ID/resolve
```

Expected response:

```json
{
  "forwarded": true,
  "approval_required": true,
  "approval_request_id": "APPROVAL_ID",
  "status": "APPROVED"
}
```

### 6. Refresh MCP Queue

```bash
curl -s "http://127.0.0.1:8000/mcp-gateway/pending-requests?approval_request_id=APPROVAL_ID"
```

Expected status:

```text
FORWARDED
```

On Android, after approval completion, returning to MCP Queue should show the refreshed forwarded state.

### 7. Verify idempotency

Run resolve again:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp-gateway/approvals/APPROVAL_ID/resolve
```

Expected behavior:

```text
forwarded=false
reason includes already final
```

Hard rule: the downstream MCP server must not receive a duplicate call.

### 8. Verify audit trail

```bash
curl -s http://127.0.0.1:8000/audit
```

Expected event types:

```text
mcp.approval_requested
approval.decision
FORWARDED_AFTER_APPROVAL
```

## Android API base URL

The Android app defaults to the emulator-friendly backend URL:

```text
http://10.0.2.2:8000/
```

That is correct for the Android emulator. It is wrong for a physical Android phone because `10.0.2.2` is an emulator alias, not your Mac or backend machine.

For a real phone demo, build with a backend URL reachable from the phone:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

Example:

```bash
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.42:8000/
```

The URL must end with `/` because Retrofit requires a trailing slash for base URLs.

## Android demo checklist

Use this only after backend validation passes.

1. Start backend with `bash scripts/run_demo_server.sh` for physical phone demo, or start manually with auth enabled/disabled as needed.
2. Confirm the backend is reachable from the Android device or emulator.
3. For emulator, the default `http://10.0.2.2:8000/` is enough.
4. For physical phone, build with `-PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/`.
5. If auth is enabled, open `Acct` and login/register first.
6. Submit a mutating MCP call through the backend API.
7. Open Android MCP Queue.
8. Confirm the pending row appears as `WAITING_FOR_APPROVAL`.
9. Tap `Open linked approval`.
10. Approve the request.
11. Confirm approval completion message appears.
12. Return to MCP Queue.
13. Confirm the queue refreshes and shows `FORWARDED`.
14. Open Audit.
15. Verify audit contains the approval and forwarding trail.

## Demo failure interpretation

| Failure | Meaning |
| --- | --- |
| Request forwards before approval | Gateway wait-mode is broken. Stop the demo. |
| No pending request appears | Queue persistence or pending API is broken. |
| Android cannot reach backend | Wrong API base URL, wrong LAN IP, backend not bound to `0.0.0.0`, firewall, or network issue. |
| Android shows missing/invalid bearer token | Auth is enabled but the app is not logged in, token expired, or Account flow did not save token. |
| Approval lacks `approved_payload_hash` | Integrity freeze is broken. Gateway should not forward. |
| Resolve returns `forwarded=false` after approval | Check resolve reason and pending status. |
| Queue stays `WAITING_FOR_APPROVAL` after resolve | Android refresh or pending status update is stale. |
| Second resolve forwards again | Idempotency is broken. This is a serious bug. |
| Missing audit event | Demo is not investor/client-safe because traceability is incomplete. |

## Brutal truth

If this runbook cannot pass cleanly, the product is not demo-ready. UI polish does not matter until this proof is reliable.
