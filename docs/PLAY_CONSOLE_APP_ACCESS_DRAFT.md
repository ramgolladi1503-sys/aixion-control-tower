# Play Console App Access Draft

This document prepares the app access information needed for Google Play review when the app requires sign-in.

Status: **draft**. Replace every TODO with real review/demo values before submission.

## App access requirement

Aixion Control Tower requires authentication before the main approval console is visible.

Google Play reviewers need a dedicated test account and a reachable review/demo backend.

## Play Console app access text

Paste a finalized version of this into Play Console after replacing TODO values:

```text
This app requires sign-in.

Use the reviewer credentials below:

Email: TODO_REVIEWER_EMAIL
Password: TODO_REVIEWER_PASSWORD

Review backend:
TODO_REVIEW_BACKEND_URL

After signing in:
1. Open the approval dashboard.
2. Select a pending approval request.
3. Review the approval detail screen.
4. Approve or reject the request if the action is enabled.
5. Open the Account screen.
6. Open Privacy/Data Controls.
7. Confirm the account deletion request path is visible.

The review backend contains demo data only. No real customer data, secrets, or production repository credentials are used in the review environment.
```

## Required values

| Field | Value | Status |
| --- | --- | --- |
| Reviewer email | TODO | Missing |
| Reviewer password | TODO | Missing |
| Review backend URL | TODO | Missing |
| Privacy policy URL | TODO | Missing |
| Account deletion URL | TODO | Missing |
| Demo account role | TODO | Missing |
| Demo approval data available | TODO | Missing |
| App build version/code | TODO | Missing |

## Reviewer account rules

The reviewer account must be:

```text
dedicated to Google Play review
safe to share in Play Console
connected to demo data only
not a personal account
not a production owner account
not capable of destructive production actions
```

Recommended role for first review build:

```text
REVIEWER or MAINTAINER
```

Use OWNER only if owner-only screens are required for review and the backend contains no real production data.

## Demo flow requirements

The account should show at least:

```text
one pending approval
one completed/failed/historical approval or audit item
account/privacy controls
account deletion request entry point
```

## Privacy and account deletion URLs

Before adding URLs to Play Console, verify them with:

```text
Actions -> Verify public page URLs -> Run workflow
allow_placeholders=false
```

Do not submit URLs that still contain TODO values.

## Backend access rules

The review backend must be:

```text
publicly reachable over HTTPS
stable during review
not dependent on same-Wi-Fi laptop/mobile setup
not localhost/LAN only
not using real customer data
```

## What not to do

Do not provide:

```text
personal account credentials
production owner credentials
credentials that expose real secrets/customer data
LAN-only backend instructions
manual steps that require contacting the developer during review
```

Google Play review should be able to complete without asking for extra access.
