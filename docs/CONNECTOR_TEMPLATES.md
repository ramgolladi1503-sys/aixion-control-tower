# Connector Templates

This document defines PR 109: ready-made connector templates for bring-your-own-agent setup.

## Purpose

The connector registry, credentials, webhook ingress, and schema mapper make external agents technically possible. Templates make them usable.

Without templates, every owner has to manually design connector defaults and mapper paths. That is unnecessary friction.

## Owner APIs

List templates:

```http
GET /connectors/templates
```

Get one template:

```http
GET /connectors/templates/{template_id}
```

## Templates included

```text
openclaw-local-bridge
antigravity-workspace-bridge
gemini-custom-agent
claude-cursor-agent
local-agent-bridge
```

## What a template contains

Each template includes:

```text
id
display_name
description
provider_label
connector_type
auth_type
connector_defaults
mapper
sample_payload
setup_notes
tags
```

## Example use flow

```text
1. Owner lists templates.
2. Owner chooses OpenClaw, Antigravity, Gemini, Claude/Cursor, or local bridge.
3. UI pre-fills connector creation form from connector_defaults.
4. UI pre-fills schema mapper from mapper.
5. Owner adds project/repository scopes.
6. Owner issues a connector secret.
7. Owner previews sample payload.
8. Agent can call the generic webhook.
```

## Security note

Templates do not grant access by themselves. They are presets only. The actual connector still needs scopes, credentials, and enabled status.

## Scope boundary

This PR does not add Android UI or sample event simulation. It only exposes backend template catalog data.

## Next PR

PR 110 should add Android connector management so owners can use these templates from the phone.
