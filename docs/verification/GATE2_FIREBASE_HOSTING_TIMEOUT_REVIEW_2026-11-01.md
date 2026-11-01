# Gate 2 Firebase Hosting timeout review — 2026-11-01

## Finding

Firebase Hosting's documented 60-second rewrite timeout does not, by itself,
truncate the current Codex or Antigravity approval wait. In both checked-in
hooks, the host process keeps the same invocation alive and polls the API.
Each individual HTTP request is capped at 10 seconds:

- `integrations/antigravity/aixion_pre_tool_hook.py` sets
  `REQUEST_TIMEOUT_SECONDS = 10`.
- `integrations/codex/aixion_permission_request_hook.py` sets
  `MAX_REQUEST_TIMEOUT_SECONDS = 10.0`.
- Both hooks use a two-second decision polling interval and invocation-scoped
  heartbeat/consume operations.

This is source inspection only. It does not prove Firebase Hosting routing,
deployed HTTPS, or a real provider/device round trip.

## Limitation

The MCP JSON-RPC transport is different: its server handler waits
synchronously for the approval decision, bounded by the configured approval
TTL. A request waiting longer than Firebase Hosting's 60-second proxy limit
would be terminated with a gateway timeout. Therefore, placing a catch-all
Firebase Hosting rewrite in front of the entire API would not preserve the
existing long-wait MCP behavior.

## Cost and architecture decision

Firebase Hosting may be a lower-cost front door for the Android app and the
current Codex/Antigravity REST approval protocol. It is not yet an approved
replacement for the API load balancer because the current API also exposes the
long-wait MCP route. Preserving that route would require either a separately
routed endpoint and verified client configuration, or retaining the current
load-balancer architecture.

The latest recorded live cloud preflight was for the staging account on
2026-10-31; it found billing disabled and no staging Cloud Run service. That
result is not a fresh production-account check. This repository has no Firebase
Hosting project configuration. No DNS, billing, Firebase, Cloud Run,
load-balancer, or other cloud state was changed.

## Evidence and next verification

Reviewed at repository commit
`548c8d06315b1687bb5a2941060197a47bb1c21b`:

- Antigravity and Codex hook constants and their decision-poll loops.
- `backend/app/mcp_transport_routes.py`, which performs the synchronous MCP
  approval wait.
- `deploy/gcp/cloud-run-staging.yaml`, which sets a 960-second Cloud Run
  request timeout to cover that server-side wait.
- Google documentation states that Firebase Hosting rewrites to Cloud Run
  have a 60-second request timeout:
  <https://firebase.google.com/docs/hosting/cloud-run>

Before selecting Firebase Hosting, validate the exact route split, authentication
header handling, response semantics, API base URL in both adapters and Android,
and the MCP endpoint behavior in an isolated staging environment. Keep the
production startup guard closed until PostgreSQL authority and the other locked
gates pass.
