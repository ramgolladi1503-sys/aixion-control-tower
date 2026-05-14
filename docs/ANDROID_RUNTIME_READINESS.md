# Android Runtime Readiness Screen

The Android app now exposes backend runtime readiness from the mobile operator surface.

This uses the backend endpoint:

```text
GET /ops/readiness
```

## Why this exists

Aixion Control Tower is a mobile-first control tower. Backend readiness is not useful enough if operators must leave the phone, open docs, or run curl to know whether the system is healthy.

The Android runtime readiness screen gives the operator a direct view of:

```text
profile
auth_enabled
database reachability
migration status
recovery snapshot availability
GitHub token configured boolean
FCM server key configured boolean
warnings
errors
```

## Security rule

The screen shows booleans only for secrets.

It must not show:

```text
database path
GitHub token value
FCM server key value
recovery snapshot content
user/session/invite/device data
```

## Android route

The screen is exposed as:

```text
Route.Ops
```

Bottom navigation label:

```text
Ops
```

## What this does not do

This PR does not add:

```text
backend changes
push alerts
monitoring stack
release packaging
incident workflows
```

Hard truth: this is visibility, not observability. Real observability still needs metrics, alerting, traces, and production dashboards.
