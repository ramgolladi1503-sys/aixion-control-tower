# External Agent Setup Validation

This runbook is the bridge between backend proof and real external-agent usage.

PR #176 proves the backend/operator loop:

```text
sample payload
-> connector webhook
-> AgentTask
-> linked Approval
-> operator decision
-> AgentTask status propagation
```

This document explains what must be true before claiming a real ChatGPT/Codex integration works outside local tests.

## Hard boundary

Do not claim production ChatGPT/Codex connectivity until all of this is true:

```text
public HTTPS backend exists
backend auth is configured intentionally
connector credential is issued and stored in the external tool
ChatGPT/Codex can call the backend from outside the laptop
Agent Work shows submitted task
Approvals shows linked decision request
operator decision changes AgentTask status
```

Local backend tests are valuable. They are not the same as a real deployed external-agent integration.

## Product loop to validate

```text
External tool submits work
-> Aixion records Agent Work
-> risky work links to Approval
-> Android operator approves / denies / requests revision
-> Agent Work status updates
-> agent/worker continues only if approved
```

## Source-of-truth docs

Use these together:

```text
docs/CONNECTED_AGENT_INTERACTION_ROADMAP.md
docs/CONNECTOR_E2E_VALIDATION.md
docs/gpt-actions/README.md
docs/gpt-actions/openapi.yaml
docs/samples/chatgpt-actions-bridge-payload.json
docs/samples/codex-agent-bridge-payload.json
```

## Public backend prerequisites

Minimum deploy requirements:

```text
AIXION backend reachable over HTTPS
valid TLS certificate
stable base URL, for example https://api.example.com
production/demo auth decision documented
CORS/proxy rules do not block external requests
backend logs visible
Android app points to the same backend URL
```

Reject setup if:

```text
server URL is localhost
server URL is LAN-only IP
backend is exposed without auth by accident
connector credential is pasted into repo files
approval decision endpoints are exposed to GPT Actions
admin/reset/owner routes are exposed to GPT Actions
```

## ChatGPT Actions setup validation

OpenAI GPT Actions let a Custom GPT call external REST APIs from natural language using an OpenAPI schema and configured authentication. In Aixion, the GPT Actions contract must stay narrow: create/read AgentTasks and append AgentTask events only.

### Required Aixion setup

```text
1. Deploy Aixion backend over public HTTPS.
2. Create/login an external-agent-safe account or use scoped demo auth intentionally.
3. Confirm GET /projects works with the token.
4. Confirm docs/gpt-actions/openapi.yaml server URL is changed from https://YOUR_AIXION_BACKEND_HOST to the deployed backend.
5. Configure bearer authentication in the GPT Action.
6. Import docs/gpt-actions/openapi.yaml into the Custom GPT action builder.
7. Test listProjects first.
8. Test createAgentTask with a low-risk docs task.
9. Confirm the task appears in Android Agent Work.
10. Create/link an Approval and confirm Android Approvals shows it.
```

### GPT Action instructions

Use instructions like this inside the Custom GPT:

```text
You are an external work-submission agent connected to Aixion Control Tower.
You may create AgentTasks and append progress events.
You must not approve, deny, execute, merge, reset, or mutate admin state.
When work requires implementation or risk, create an AgentTask first and wait for Aixion approval.
Do not claim work is approved until Aixion shows APPROVED.
Always include project, repository, branch preference, risk hint, source session, and useful metadata when available.
```

### Safe operation list

```text
GET  /projects
POST /agent/tasks
GET  /agent/tasks/{task_id}
POST /agent/tasks/{task_id}/events
```

### Unsafe operation list

Never expose these through GPT Actions:

```text
POST /approvals/{approval_id}/approve
POST /approvals/{approval_id}/deny
owner role management
invite/session revocation
connector secret issue/rotate/revoke
GitHub execution runner routes
MCP admin/registry routes
reset/demo seed routes
ops/recovery routes
```

If ChatGPT can approve its own task, the product becomes approval theater.

## ChatGPT validation checklist

```text
[ ] public backend URL works from outside local network
[ ] docs/gpt-actions/openapi.yaml imported successfully
[ ] bearer auth configured in the GPT Action
[ ] GET /projects succeeds from GPT Action test console
[ ] POST /agent/tasks creates provider=CHATGPT task
[ ] Android Agent Work shows the task
[ ] task has source/session metadata
[ ] approval is linked manually or through the bridge
[ ] Android Approvals shows the linked request
[ ] approve decision changes AgentTask status to APPROVED
[ ] deny decision changes AgentTask status to DENIED
[ ] revision decision moves AgentTask back to PLANNING
[ ] audit/events show the complete path
```

## Codex bridge setup validation

Aixion currently has a Codex connector template and sample Codex payload. This proves the backend can normalize a Codex-style request. It does not automatically mean OpenAI Codex is configured to call this backend.

### Required setup

```text
1. Deploy backend over public HTTPS.
2. Create Codex Agent Bridge connector from template.
3. Apply mapper with the correct project_id default.
4. Issue connector credential.
5. Store credential in the Codex-side bridge/tool environment, not in git.
6. Send docs/samples/codex-agent-bridge-payload.json to /connectors/{connector_id}/webhook.
7. Confirm provider=CODEX AgentTask appears in Agent Work.
8. Create/link an approval.
9. Confirm Android Approvals shows the request.
10. Submit operator decision and verify AgentTask status propagation.
```

### Codex validation checklist

```text
[ ] Codex Agent Bridge connector created
[ ] connector credential issued
[ ] mapper applied with project_id default
[ ] sample Codex payload accepted by webhook
[ ] AgentTask provider is CODEX
[ ] repository and branch preference are present
[ ] requires_approval=true
[ ] linked Approval is created
[ ] Android Agent Work shows Open linked approval
[ ] Android Approvals shows decision request
[ ] approval decision updates AgentTask status
```

## Manual curl proof

Follow the complete curl flow in:

```text
docs/CONNECTOR_E2E_VALIDATION.md
```

For ChatGPT sample payload:

```bash
curl -s -X POST "$AIXION_BASE_URL/connectors/$CONNECTOR_ID/webhook" \
  -H "Authorization: Bearer $CONNECTOR_SECRET" \
  -H 'Content-Type: application/json' \
  -d @docs/samples/chatgpt-actions-bridge-payload.json
```

For Codex sample payload:

```bash
curl -s -X POST "$AIXION_BASE_URL/connectors/$CONNECTOR_ID/webhook" \
  -H "Authorization: Bearer $CONNECTOR_SECRET" \
  -H 'Content-Type: application/json' \
  -d @docs/samples/codex-agent-bridge-payload.json
```

## Evidence to collect

Before demo or release claim, capture:

```text
backend public URL
connector id
provider label
credential issue time, not the secret itself
request timestamp
task id
approval id
Android Agent Work screenshot
Android Approvals screenshot
audit event ids
task event timeline
final task status after decision
```

Never capture or commit:

```text
raw connector secret
owner personal session token
keystore password
private backend env values
```

## Safe claims

Allowed after PR #176 and this runbook:

```text
Aixion has a tested backend connector approval loop and documented external setup path for ChatGPT/Codex-style agents.
```

Allowed only after real deployed validation:

```text
Aixion has a working deployed ChatGPT/Codex approval bridge against this backend URL.
```

Not allowed yet:

```text
Aixion is a production marketplace connector for ChatGPT/Codex.
Aixion has proven autonomous Codex execution after approval.
Aixion is enterprise-ready credential governance.
```

## Failure triage

### GPT Action import fails

Check:

```text
OpenAPI version is accepted
server URL is HTTPS
operationIds are unique
schema has no localhost URL
bearer auth is configured
```

### GPT Action auth fails

Check:

```text
Authorization header is Bearer token
backend token is not expired
role has permission to list projects/create tasks
backend auth mode matches the expected demo/production mode
```

### Webhook returns 401

Check:

```text
connector is enabled
credential was issued
secret was copied exactly once
Authorization header uses Bearer prefix
secret was not revoked or rotated after copy
```

### Agent Work does not show task

Check:

```text
webhook returned task_id
task exists in GET /agent/tasks/{task_id}
Android points to same backend URL
Android auth session is valid
provider/status filters are not hiding the task
```

### Approval does not show

Check:

```text
approval was actually created or linked
AgentTask has approval_request_id
Approvals filter is not hiding the item
risk engine did not block the item into a different state
```

## Completion definition

This setup is complete only when a human can perform the full loop without developer explanation:

```text
create connector
issue credential
configure external tool
send task
see Agent Work
open linked Approval
approve/deny/revise
see AgentTask status update
review audit evidence
```

If any step requires guessing, the setup is not release-quality yet.