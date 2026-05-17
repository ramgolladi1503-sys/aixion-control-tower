# Connector E2E Validation

This guide proves the connected-agent loop instead of only documenting it.

The validation target is:

```text
ChatGPT / Codex sample payload
-> Connector webhook
-> AgentTask appears in Agent Work
-> Approval is linked
-> operator approves / denies / requests revision
-> AgentTask status updates
-> audit/events prove the path
```

## What this does prove

```text
Connector templates exist.
A connector can be created from a template.
A credential can be issued.
The selected template mapper can normalize the external payload.
The webhook can create an AgentTask.
The AgentTask is visible through `/agent/tasks`.
A linked approval can be created through `/agent/tasks/{task_id}/approval`.
Approval decisions propagate back to AgentTask status.
Events and audit records exist for the loop.
```

## What this does not prove

```text
A public HTTPS backend is deployed.
A real Custom GPT has been configured in ChatGPT.
A real Codex agent has been configured externally.
A worker has executed code after approval.
A Play Store reviewer can reach the backend.
```

Do not overclaim this as live external-agent production readiness. This is a backend/operator proof of the connector approval loop.

## Automated regression

Run:

```bash
cd backend
python -m pytest tests/test_connector_e2e_validation_flow.py
```

Recommended broader checks:

```bash
cd backend
python -m pytest tests/test_connector_templates.py tests/test_agent_task_approval_bridge.py tests/test_connector_e2e_validation_flow.py
python -m pytest
```

## Manual validation flow

### 1. Start backend

```bash
cd backend
AIXION_AUTH_ENABLED=false uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 2. Create or seed a project

Use any existing project id, or create one:

```bash
curl -s -X POST http://localhost:8000/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Connector E2E Demo",
    "description": "External agent callback validation",
    "mode": "STRICT",
    "rules": ["External agent work must require approval"]
  }'
```

Save the returned `id` as:

```text
PROJECT_ID=<returned project id>
```

### 3. Fetch the ChatGPT template

```bash
curl -s http://localhost:8000/connectors/templates/chatgpt-actions-bridge
```

Confirm:

```text
provider_label = CHATGPT
connector_type = GPT_ACTIONS
auth_type = BEARER
mapper.defaults.requires_approval = true
```

### 4. Create connector from template defaults

Create a connector using the template defaults, scoped to the project and repo:

```bash
curl -s -X POST http://localhost:8000/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "ChatGPT Actions Bridge",
    "connector_type": "GPT_ACTIONS",
    "provider_label": "CHATGPT",
    "auth_type": "BEARER",
    "allowed_project_ids": ["PROJECT_ID"],
    "allowed_repositories": ["owner/repo"],
    "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
    "rate_limit_per_minute": 60,
    "enabled": true,
    "config": {"profile": "chatgpt-actions", "mode": "approval-gated"}
  }'
```

Replace `PROJECT_ID` before running.

Save the returned connector id:

```text
CONNECTOR_ID=<returned connector id>
```

### 5. Apply mapper with project default

The template payload does not know your project id. Add it as a mapper default:

```bash
curl -s -X PUT http://localhost:8000/connectors/$CONNECTOR_ID/schema-mapper \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "default_action": "CREATE_AGENT_TASK",
    "field_paths": {
      "title": "task.title",
      "goal": "task.goal",
      "context": "task.context",
      "repository": "target.repository",
      "branch_preference": "target.branch",
      "metadata": "metadata"
    },
    "defaults": {
      "project_id": "PROJECT_ID",
      "requested_action": "GENERATE_DOCS",
      "requires_approval": true
    }
  }'
```

Replace `PROJECT_ID` before running.

### 6. Issue credential

```bash
curl -s -X POST http://localhost:8000/connectors/$CONNECTOR_ID/secret/issue \
  -H 'Content-Type: application/json' \
  -d '{"note": "manual connector e2e validation"}'
```

Save the returned one-time credential:

```text
CONNECTOR_SECRET=<returned secret>
```

### 7. Send ChatGPT-style payload

```bash
curl -s -X POST http://localhost:8000/connectors/$CONNECTOR_ID/webhook \
  -H "Authorization: Bearer $CONNECTOR_SECRET" \
  -H 'Content-Type: application/json' \
  -d @docs/samples/chatgpt-actions-bridge-payload.json
```

Expected:

```text
accepted = true
action = CREATE_AGENT_TASK
task_id is present
```

Save:

```text
TASK_ID=<returned task id>
```

### 8. Verify Agent Work API visibility

```bash
curl -s http://localhost:8000/agent/tasks/$TASK_ID
curl -s 'http://localhost:8000/agent/tasks?provider=CHATGPT'
```

Expected:

```text
provider = CHATGPT
requires_approval = true
external_agent_id = CONNECTOR_ID
metadata.connector_webhook = true
```

On Android, this same task should appear in:

```text
Agent Work -> All
Agent Work -> Waiting approval if approval_required/status applies
```

### 9. Create linked approval

```bash
curl -s -X POST http://localhost:8000/agent/tasks/$TASK_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "PROJECT_ID",
    "title": "Approve connector E2E task",
    "summary": "Approval linked from connector-created AgentTask.",
    "agent_name": "CHATGPT",
    "target_branch": "feature/connector-e2e",
    "files": [
      {
        "path": "docs/connector-e2e-demo.md",
        "change_type": "update",
        "diff": "+ connector e2e demo",
        "new_content": "connector e2e demo\n"
      }
    ],
    "test_plan": ["python -m pytest backend/tests/test_connector_e2e_validation_flow.py"],
    "rollback_plan": "Deny the approval or revert the proposed branch."
  }'
```

Replace `PROJECT_ID` before running.

Expected:

```text
approval id is returned
source_provider = CHATGPT
verified_source = true
```

On Android, this approval should appear in:

```text
Approvals -> Action
```

The Agent Work card should expose:

```text
Open linked approval
```

### 10. Approve and verify task status propagation

```bash
curl -s -X POST http://localhost:8000/approvals/$APPROVAL_ID/approve
curl -s http://localhost:8000/agent/tasks/$TASK_ID
curl -s http://localhost:8000/agent/tasks/$TASK_ID/events
```

Expected:

```text
approval.status = APPROVED
agent_task.status = APPROVED
events include TASK_CREATED, APPROVAL_CREATED, APPROVED
```

## Codex validation

Use the same flow with:

```text
template: codex-agent-bridge
sample payload: docs/samples/codex-agent-bridge-payload.json
provider expected: CODEX
```

The Codex sample should create an AgentTask with:

```text
provider = CODEX
repository = owner/repo
branch_preference = feature/codex-task
requires_approval = true
```

## Evidence checklist

Record the following before claiming the connector loop works:

```text
[ ] backend test `test_connector_e2e_validation_flow.py` passes
[ ] connector created from ChatGPT template
[ ] credential issued
[ ] mapper applied with project_id default
[ ] webhook accepted ChatGPT sample payload
[ ] AgentTask visible through `/agent/tasks`
[ ] Android Agent Work shows the task
[ ] linked Approval created
[ ] Android Approvals shows the request
[ ] approve/deny/revise decision submitted
[ ] AgentTask status changed after decision
[ ] events show task creation, approval creation, and decision propagation
[ ] audit contains connector webhook, approval creation, approval decision, and propagation events
```

## Safe claim after this passes

```text
Aixion has a tested connector approval loop: a configured ChatGPT/Codex-style connector can create Agent Work, link an Approval, and receive task status propagation after the mobile/operator decision.
```

## Unsafe claim after this passes

```text
ChatGPT/Codex is fully connected in production.
```

That claim requires public HTTPS deployment and real external-agent configuration.