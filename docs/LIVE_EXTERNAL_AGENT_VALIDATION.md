# Live External Agent Validation

This is the first validation step after the backend passes:

```text
GET /ops/external-agent-readiness
```

It does not configure ChatGPT or Codex for you. It gives you a repeatable smoke test against a deployed backend.

## Script

```text
backend/scripts/validate_live_external_agent.py
```

The script checks:

```text
/health
/ops/external-agent-readiness
optional connector webhook sample payload
optional AgentTask visibility check
```

## Environment

Required for readiness-only validation:

```bash
export AIXION_BASE_URL="https://YOUR_PUBLIC_AIXION_BACKEND"
export AIXION_OWNER_TOKEN="OWNER_OR_MAINTAINER_BEARER_TOKEN"
```

Required for webhook validation:

```bash
export AIXION_CONNECTOR_ID="connector_id"
export AIXION_CONNECTOR_SECRET="connector_secret"
```

Never commit these values.

## Readiness-only check

```bash
cd backend
python scripts/validate_live_external_agent.py --skip-webhook
```

Expected result:

```json
{
  "checks": [
    {"name": "health", "ok": true},
    {"name": "external_agent_readiness", "ok": true},
    {"name": "connector_webhook", "ok": true}
  ]
}
```

## ChatGPT sample webhook check

```bash
cd backend
python scripts/validate_live_external_agent.py --provider chatgpt
```

This sends:

```text
docs/samples/chatgpt-actions-bridge-payload.json
```

Expected result:

```text
connector webhook accepted task <task_id>
AgentTask <task_id> is visible
```

## Codex sample webhook check

```bash
cd backend
python scripts/validate_live_external_agent.py --provider codex
```

This sends:

```text
docs/samples/codex-agent-bridge-payload.json
```

Expected result:

```text
connector webhook accepted task <task_id>
AgentTask <task_id> is visible
```

## What this proves

```text
A deployed backend is reachable.
External-agent readiness gate passes.
A configured connector accepts a sample payload.
The webhook creates an AgentTask.
The AgentTask can be read back through the API.
```

## What this does not prove

```text
A real Custom GPT is configured.
A real Codex worker is configured.
Android displays the created AgentTask.
A linked Approval was created.
A mobile operator decision propagated.
```

Those still require the full live demo path.

## Safe claim after this passes

```text
The deployed backend and connector can accept a ChatGPT/Codex-style sample payload and create visible Agent Work.
```

## Unsafe claim after this passes

```text
ChatGPT/Codex production integration is complete.
```

That requires configuring the actual external tool and finishing the mobile approval decision path.

## Validation tests

```bash
cd backend
python -m pytest tests/test_live_external_agent_validator.py
python -m pytest tests/test_live_external_agent_validator.py tests/test_external_agent_readiness.py
python -m pytest
```

## Failure guide

### health fails

Check:

```text
AIXION_BASE_URL is correct
backend is running
TLS certificate is valid
network can reach the backend
```

### readiness fails

Check:

```text
AIXION_PUBLIC_BASE_URL is configured on backend
public URL is HTTPS
public URL is not localhost/LAN/private IP
auth is enabled or explicit demo override is configured
required connector templates exist
```

### webhook fails with 401

Check:

```text
connector id is correct
connector secret was copied correctly
secret was not revoked/rotated
authorization header is Bearer <secret>
```

### task visibility fails

Check:

```text
AIXION_OWNER_TOKEN is valid
owner token belongs to user with access
created task id exists
backend auth mode matches expectation
```
