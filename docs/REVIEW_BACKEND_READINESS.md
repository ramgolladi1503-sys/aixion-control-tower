# Review Backend Readiness

This document defines the minimum backend and app-access requirements for Phase 0 Google Play/internal testing review.

This is **not** the full Phase 1 production/money infrastructure plan. Phase 1 still needs real production hosting, domain, database, monitoring, billing, support operations, and commercial policies.

## Goal

Before an Android build is sent to Google Play review or internal testing, reviewers must be able to open the app, sign in, see realistic demo data, verify privacy/account controls, and exercise the core approval flow against a stable HTTPS backend.

## Required review environment

| Area | Requirement | Status |
| --- | --- | --- |
| Backend URL | Public HTTPS URL reachable from Google Play review devices | TODO |
| Backend health check | Endpoint returns 200 without login | TODO |
| Demo account | Dedicated reviewer account, not a personal owner account | TODO |
| Demo data | At least one pending approval and one historical/audit item | TODO |
| Auth flow | Login works with reviewer credentials | TODO |
| Privacy controls | Account screen exposes privacy/data controls | TODO |
| Account deletion request | Reviewer can trigger or inspect request path safely | TODO |
| Public privacy URL | HTTPS privacy page loads without login | TODO |
| Public account deletion URL | HTTPS deletion page loads without login | TODO |
| Signed/release build | Android build points to the review backend, not localhost/LAN | TODO |

## Backend URL rules

The review build must not point to:

```text
localhost
127.0.0.1
10.0.2.2
192.168.x.x
172.16.x.x
10.x.x.x
plain HTTP LAN URLs
```

Acceptable review backend shape:

```text
https://review-api.example.com
https://api.example.com
https://aixion-control-tower-review.example.com
```

For Phase 0, the backend may be a review/demo deployment as long as it is public HTTPS, stable enough for review, and contains no real secrets or customer data.

## Required backend health behavior

Recommended endpoint:

```text
GET /health
```

Minimum expected response:

```text
HTTP 200
```

Recommended response body:

```json
{
  "status": "ok",
  "service": "aixion-control-tower-backend",
  "environment": "review"
}
```

Do not expose secrets, database URLs, signing keys, GitHub tokens, FCM keys, or internal infrastructure details in health responses.

## Reviewer test account

Use a dedicated account only for Google Play/app review.

Recommended shape:

```text
Email: reviewer.demo@example.com
Role: REVIEWER or MAINTAINER depending on visible flows
Password: stored only in Play Console app access instructions
```

Do not use:

```text
personal Gmail account
real production owner account
admin account with destructive access
account containing private repository/customer data
```

## Demo data requirements

The review backend should include safe demo data:

```text
Demo Project
Pending approval request
Approved or rejected historical request
Audit/history item
Account privacy controls visible
Account deletion request path available
```

Safe examples:

```text
Approval Request #1042
Review backend configuration update
Generate release evidence bundle
Demo Project
Connected Agent Task
```

Unsafe examples:

```text
real API keys
real GitHub tokens
real customer names
private repositories
production branch names containing sensitive work
personal emails beyond the reviewer account
```

## Google Play app access instructions

Paste a concise version of this into Play Console when required:

```text
This app requires sign-in.

Use the provided reviewer credentials to sign in.

After login:
1. Open the approval dashboard.
2. Select a pending approval request.
3. Review the details.
4. Approve or reject the request if available.
5. Open Account > Privacy/Data Controls.
6. Confirm the account deletion request entry point is visible.

The backend is a review/demo environment with safe demo data only.
```

Required values before submission:

```text
Reviewer email: TODO
Reviewer password: TODO
Review backend URL: TODO
Privacy policy URL: TODO
Account deletion URL: TODO
```

## Android build requirements

Before signing the AAB/APK for review:

```text
[ ] Backend base URL points to review HTTPS backend
[ ] Build does not point to LAN/local/dev backend
[ ] Build uses release/review-safe configuration
[ ] No debug banner or stack traces are visible
[ ] App starts at auth screen when no session exists
[ ] Login succeeds with reviewer account
[ ] Privacy/data controls are reachable from Account screen
[ ] Account deletion request flow does not crash
```

## Public URL verification

Use the public page URL smoke workflow added for Phase 0:

```text
Actions -> Verify public page URLs -> Run workflow
```

For deployment-only smoke testing while TODO values remain:

```text
allow_placeholders=true
```

For final Play submission:

```text
allow_placeholders=false
```

Final mode must pass before URLs are submitted to Google Play.

## Review backend smoke checklist

Before Play/internal testing upload:

```text
[ ] GET /health returns 200 from public internet
[ ] Android app can reach backend over HTTPS
[ ] Reviewer credentials work
[ ] Dashboard loads demo approvals
[ ] Approval detail screen loads
[ ] Approve/reject action is safe in demo data
[ ] Audit/history data is visible
[ ] Account/privacy controls load
[ ] Account deletion request path works safely
[ ] No real secrets or customer data appear
[ ] Public privacy URL loads
[ ] Public account deletion URL loads
```

## What this does not solve

This document does not provide:

```text
real production domain
paid production database
billing/subscriptions
customer support process
monitoring/alerting stack
backup/restore process
legal/privacy approval
commercial terms/refund policy
```

Those belong to Phase 1 production and money readiness.

## Hard rule

A mobile build that only works on laptop + phone on the same Wi-Fi is a demo build, not a Play-review-ready build.

For Play/internal testing review, the app must reach a public HTTPS backend that a reviewer can access from outside your network.
