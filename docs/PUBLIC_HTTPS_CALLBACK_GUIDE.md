# Public HTTPS Callback Guide

This guide explains how to make Aixion reachable from ChatGPT, Custom GPT Actions, Codex-style workers, Android devices, and external MCP/agent callbacks.

## Why this exists

A laptop-only backend is not enough.

```text
localhost backend works for local development
10.0.2.2 works for Android emulator
real phones need a network-reachable backend
ChatGPT/GPT Actions need public HTTPS
external workers need public HTTPS or private network access
FCM push needs registered Android devices and backend dispatch
```

Hard truth: if Aixion is only running on `localhost`, it is not a connected-agent control tower. It is a local demo.

## Supported modes

### Local development

Use this when building backend or Android locally.

```text
backend URL for local machine: http://127.0.0.1:8000
Android emulator URL:         http://10.0.2.2:8000
real Android phone URL:       http://<your-laptop-LAN-IP>:8000
GPT Actions URL:              not supported from localhost
```

Local mode is useful for UI/API development, but external agents cannot reliably call it.

### Demo tunnel

Use this for short demos where ChatGPT or a real phone must reach your local backend.

```text
local backend -> HTTPS tunnel -> public URL
```

Acceptable examples:

```text
ngrok
Cloudflare Tunnel
Tailscale Funnel
localhost.run
```

Demo tunnel requirements:

```text
HTTPS URL
short-lived token/session
AIXION_PROFILE=demo or local only when appropriate
never expose unsafe reset/admin routes without auth in public demos
update GPT Actions server URL to tunnel URL
update Android API base URL to tunnel URL for real device testing
```

### Production deployment

Use this when Aixion is expected to work away from your laptop.

Required:

```text
public HTTPS backend
AIXION_PROFILE=production
AIXION_AUTH_ENABLED=true
AIXION_DB_PATH configured to a non-demo DB path
GITHUB_TOKEN configured if GitHub execution features are enabled
FCM_SERVER_KEY configured if push notifications are expected
stable domain name
valid TLS certificate
log retention
audit retention
backup/recovery plan
```

The current backend startup validation already treats these production variables as required:

```text
AIXION_DB_PATH
GITHUB_TOKEN
FCM_SERVER_KEY
```

## Environment checklist

### Demo tunnel checklist

```text
backend running locally
HTTPS tunnel active
/tunnel-url/health returns ok
Aixion user can log in
Android app points at tunnel backend
Custom GPT/OpenAPI server URL points at tunnel backend
Authorization header is configured
POST /agent/tasks works
POST /agent/tasks/{task_id}/approval works
Android shows task and approval
notification record is created
push status is SENT, SKIPPED, NO_DEVICE, or FAILED with readable reason
```

### Production checklist

```text
AIXION_PROFILE=production
AIXION_AUTH_ENABLED=true
AIXION_DB_PATH=/safe/persistent/path/aixion.sqlite3
GITHUB_TOKEN=<configured>
FCM_SERVER_KEY=<configured>
HTTPS domain configured
CORS/reverse proxy policy reviewed
rate limits configured at proxy/platform
logs available
backups available
runtime readiness endpoint checked
no demo/test DB file is used
```

## GPT Actions setup

After deployment or tunnel setup, update the OpenAPI server URL:

```yaml
servers:
  - url: https://YOUR_PUBLIC_AIXION_BACKEND
```

Then test in this order:

```text
GET /projects
POST /agent/tasks
GET /agent/tasks/{task_id}
POST /agent/tasks/{task_id}/events
```

Do not expose unsafe endpoints through GPT Actions:

```text
approval decisions
owner/admin role changes
session revocation
reset/demo seed routes
GitHub execution runner routes
ops/recovery routes
MCP admin routes
```

The external GPT should submit work and report evidence. It should not approve or execute privileged operations by itself.

## Android setup

Android must point at a backend URL reachable from the device.

```text
emulator:       http://10.0.2.2:8000
same Wi-Fi:     http://<laptop-ip>:8000
public demo:    https://<tunnel-host>
production:     https://<production-domain>
```

Validation:

```text
login succeeds
GET /agent/tasks succeeds
approval list loads
notification list loads
FCM device registration succeeds
AgentTask approval creates a notification record
push_status is understandable
```

## FCM / phone notification validation

Notification records are not the same as real push delivery.

Check this chain:

```text
Android obtains FCM token
Android registers token with POST /notifications/devices
backend stores DeviceRegistration
AgentTask lifecycle creates Notification
backend dispatch_push runs
Notification.push_status changes
phone receives push
push opens useful screen or notification inbox
```

Current expected push statuses:

```text
PENDING  -> notification created before dispatch result
NO_DEVICE -> no registered device matched notification target
SKIPPED -> FCM_SERVER_KEY missing
FAILED -> FCM/API/network error
SENT -> FCM accepted the send request
```

Hard truth:

```text
SENT does not prove the user saw the notification.
It only proves FCM accepted the request.
Android notification display and deep-link behavior still need device testing.
```

## External worker callback setup

For a worker running outside the backend process, the minimum callback permissions are:

```text
read approved AgentTask
append AgentTask events
report execution status
report validation status
report result/PR/failure evidence
```

Recommended callback sequence:

```text
GET /agent/tasks?status=APPROVED
POST /agent/tasks/{task_id}/events EXECUTION_STARTED
POST /agent/tasks/{task_id}/events TESTS_STARTED
POST /agent/tasks/{task_id}/events TESTS_PASSED or TESTS_FAILED
POST /agent/tasks/{task_id}/events READY_FOR_PR / FAILED / DONE
```

Do not let an external worker:

```text
approve its own task
merge its own PR
change users or roles
reset data
change production secrets
bypass risk policy
```

## MCP callback setup

MCP gateway clients and child servers need the same public reachability discipline.

For a demo:

```text
MCP client -> public HTTPS Aixion gateway -> approval -> child MCP/server route
```

For production:

```text
MCP client -> authenticated HTTPS gateway -> policy/risk/approval -> audited forwarding
```

Do not expose internal MCP registry/admin controls to public clients without strict auth and audit.

## Minimum end-to-end demo script

```text
1. Start backend.
2. Expose backend through HTTPS tunnel or production domain.
3. Confirm /health from outside your laptop.
4. Register/login Android device against public backend.
5. Register FCM token.
6. Configure Custom GPT Action server URL.
7. Create AgentTask through GPT Action.
8. Create linked approval.
9. Confirm Android shows AgentTask and approval.
10. Approve from Android.
11. Confirm AgentTask status becomes APPROVED.
12. Run dry-run worker.
13. Confirm AgentTask timeline shows EXECUTION_STARTED and RESULT_READY.
14. Confirm notification records exist for approval and result lifecycle.
```

## Known gaps after this guide

```text
Android deep-link routing into exact AgentTask/Approval screen
real device FCM proof screenshots/logs
production deployment manifest
external worker auth scoping
notification retry queue
rate limiting implementation
observability dashboard
real branch/PR worker execution
```

Hard truth: this guide does not deploy Aixion. It removes ambiguity about what must be true before ChatGPT, Android, workers, and MCP clients can talk to Aixion reliably.
