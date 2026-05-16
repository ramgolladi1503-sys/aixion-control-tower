# Public Page Values Intake

This document is the intake template for replacing public privacy/account deletion TODO values.

Do **not** use guessed values. Do **not** copy values from examples unless they are actually true for the release environment. Wrong privacy values are worse than visible TODOs.

## Purpose

Before `docs/public/privacy-policy.html` and `docs/public/account-deletion.html` can be submitted to Google Play, the real-world values below must be provided and reviewed.

After this intake is completed, the next PR should update:

```text
docs/PUBLIC_PAGE_RELEASE_VALUES.md
docs/public/privacy-policy.html
docs/public/account-deletion.html
```

Then run:

```bash
python scripts/validate_public_pages_ready.py
```

## Required release identity values

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Developer/operator name | TODO | TODO |
| Google Play developer account display name | TODO | TODO |
| Privacy/support email | TODO | TODO |
| Support email inbox owner | TODO | TODO |
| Last updated / effective date | TODO | TODO |

## Required public URLs

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Privacy policy public URL | TODO | TODO |
| Account deletion public URL | TODO | TODO |
| GitHub Pages deployment source | TODO | TODO |
| URL smoke-test evidence | TODO | TODO |

Expected URL shape if GitHub Pages remains the hosting path:

```text
https://ramgolladi1503-sys.github.io/aixion-control-tower/privacy-policy.html
https://ramgolladi1503-sys.github.io/aixion-control-tower/account-deletion.html
```

Do not publish these in Play Console until they return the correct public pages over HTTPS.

## Required account deletion values

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Request acknowledgement time | TODO | TODO |
| Deletion/disablement processing time | TODO | TODO |
| Whether account is disabled immediately after authenticated request | TODO | TODO |
| Whether sessions are revoked immediately | TODO | TODO |
| Whether deletion is hard-delete, anonymization, or manual operator processing | TODO | TODO |
| What data may remain after deletion | TODO | TODO |
| How users request deletion outside the app | TODO | TODO |

Recommended safe default only if actually implemented and operationally true:

```text
Authenticated deletion requests revoke active sessions and disable account access immediately. Final deletion/anonymization is completed manually by the operator within the published processing window.
```

## Required retention values

| Data type | Exact release value | Evidence / owner |
| --- | --- | --- |
| Account profile retention | TODO | TODO |
| Session retention | TODO | TODO |
| Invite retention | TODO | TODO |
| Approval/work-order retention | TODO | TODO |
| Audit log retention | TODO | TODO |
| Account deletion request retention | TODO | TODO |
| Device token retention | TODO | TODO |
| Server log retention | TODO | TODO |
| Backup retention | TODO | TODO |

Do not write vague values like:

```text
retained as needed
kept for security
stored for some time
removed soon
```

Use concrete values or concrete operator rules, for example:

```text
retained for 90 days after account deletion request, then deleted or anonymized
retained until the workspace/operator manually removes the project evidence
retained for 30 days in server logs
retained until backup expiry, no longer than 30 days
```

Only use examples that match the actual implementation and release process.

## Required backend/provider values

| Provider area | Release value | Region | Evidence / owner |
| --- | --- | --- | --- |
| Backend host/provider | TODO | TODO | TODO |
| Database provider | TODO | TODO | TODO |
| Logging/monitoring provider | TODO | TODO | TODO |
| Email provider | TODO | TODO | TODO |
| Push notification provider | TODO | TODO | TODO |
| GitHub/repository integration status | TODO | TODO | TODO |
| Analytics provider | TODO | TODO | TODO |
| Crash reporting provider | TODO | TODO | TODO |

If a provider is not used in the release build, say so clearly:

```text
Not enabled in the release/review build.
```

## Required app access values for Play review

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Reviewer test email | TODO | TODO |
| Reviewer test password | TODO | TODO |
| Review/demo backend URL | TODO | TODO |
| Demo account role | TODO | TODO |
| Demo approval data availability | TODO | TODO |
| Steps for reviewer to access privacy controls | TODO | TODO |
| Steps for reviewer to request account deletion | TODO | TODO |

Do not use a personal primary account for Play review. Use a dedicated demo account.

## Required SDK/permission review values

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Android permissions used | TODO | TODO |
| Push notification permission status | TODO | TODO |
| Internet/network usage disclosure | TODO | TODO |
| Analytics SDK status | TODO | TODO |
| Crash reporting SDK status | TODO | TODO |
| Advertising ID usage | TODO | TODO |
| Data Safety answers reviewed | TODO | TODO |

## Legal/privacy review

| Field | Value | Evidence / owner |
| --- | --- | --- |
| Legal/privacy review owner | TODO | TODO |
| Review date | TODO | TODO |
| Known risks accepted | TODO | TODO |
| Final approval to publish public pages | TODO | TODO |

## Completion checklist

Before replacing public TODO values:

```text
[ ] Developer/operator name is final
[ ] Privacy/support email is live and monitored
[ ] Public URLs are live over HTTPS
[ ] Account deletion behavior is verified against backend behavior
[ ] Retention values are concrete
[ ] Backend/provider values are accurate
[ ] Reviewer test account works
[ ] SDK/permission review is complete
[ ] Legal/privacy review is complete
[ ] No fake compliance claims are added
```

## Next PR after this intake is completed

```text
PR #144 — Replace public privacy/account deletion page values
```

Expected files:

```text
docs/PUBLIC_PAGE_RELEASE_VALUES.md
docs/public/privacy-policy.html
docs/public/account-deletion.html
```

Expected validation:

```bash
python scripts/validate_public_pages_ready.py
```

Expected passing output:

```text
Public pages have no obvious placeholders and contain required disclosure sections.
```
