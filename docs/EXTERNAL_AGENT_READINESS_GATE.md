# External Agent Readiness Gate

This document explains the backend gate exposed at:

```text
GET /ops/external-agent-readiness
```

The existing `/ops/readiness` endpoint answers a different question:

```text
Is the backend runtime healthy?
```

This endpoint answers:

```text
Is this backend safe enough to start real ChatGPT/Codex external-agent validation?
```

## Why this gate exists

Backend tests can prove the connector loop locally, but a real external-agent validation needs stricter conditions:

```text
public HTTPS base URL
non-localhost/non-private host
intentional auth posture
required ChatGPT/Codex connector templates
existing production validation errors surfaced
```

Without this gate, it is too easy to overclaim that a local or unsafe demo backend is ready for ChatGPT/Codex callbacks.

## Required environment

For a real external-agent validation run, configure:

```bash
AIXION_PUBLIC_BASE_URL=https://YOUR_PUBLIC_AIXION_BACKEND
AIXION_AUTH_ENABLED=true
```

For an intentionally unauthenticated demo only:

```bash
AIXION_PUBLIC_BASE_URL=https://YOUR_PUBLIC_DEMO_BACKEND
AIXION_AUTH_ENABLED=false
AIXION_ALLOW_UNAUTHENTICATED_EXTERNAL_AGENT_DEMO=true
```

Do not use the unauthenticated demo override for production.

## Not ready examples

These must fail:

```text
AIXION_PUBLIC_BASE_URL unset
http://localhost:8000
https://localhost
https://192.168.1.20
AIXION_AUTH_ENABLED=false without explicit demo override
missing ChatGPT/Codex connector templates
existing production settings validation errors
```

## Ready example

This can pass:

```text
AIXION_PUBLIC_BASE_URL=https://api.example.com
AIXION_AUTH_ENABLED=true
required connector templates present:
- chatgpt-actions-bridge
- codex-agent-bridge
```

## Response shape

The endpoint returns:

```json
{
  "status": "ready",
  "profile": "production",
  "public_base_url": "https://api.example.com",
  "public_base_url_configured": true,
  "public_base_url_https": true,
  "public_base_url_public_host": true,
  "auth_enabled": true,
  "unauthenticated_demo_override": false,
  "required_templates_present": true,
  "required_template_ids": ["chatgpt-actions-bridge", "codex-agent-bridge"],
  "available_template_ids": ["chatgpt-actions-bridge", "codex-agent-bridge"],
  "errors": [],
  "warnings": []
}
```

## Validation command

```bash
cd backend
python -m pytest tests/test_external_agent_readiness.py
```

Recommended broader validation:

```bash
cd backend
python -m pytest tests/test_external_agent_readiness.py tests/test_connector_e2e_validation_flow.py
python -m pytest
```

## Safe claim after this gate passes

```text
The backend is configured for external-agent validation readiness.
```

## Unsafe claim after this gate passes

```text
ChatGPT/Codex is already connected and working in production.
```

That still requires a live end-to-end validation run using the deployed backend, external tool configuration, Android Agent Work, linked Approval, and operator decision propagation.