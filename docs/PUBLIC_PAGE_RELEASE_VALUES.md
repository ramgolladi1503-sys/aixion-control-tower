# Public Page Release Values

This file lists the exact real-world values required before the public privacy/account deletion pages can be submitted to Google Play.

Do not guess these values. Do not invent them for a cleaner checklist. Wrong values are worse than visible TODOs.

## Required values

| Field | Required value | Current status |
| --- | --- | --- |
| Developer/operator name | The legal/person/entity name that matches or clearly identifies the Play Console listing owner | TODO |
| Privacy/support email | Public email users can contact for privacy/account requests | TODO |
| Last updated date | Date the published pages become accurate | TODO |
| Privacy policy public URL | HTTPS URL for `privacy-policy.html` after Pages/hosting is live | TODO |
| Account deletion public URL | HTTPS URL for `account-deletion.html` after Pages/hosting is live | TODO |
| Account deletion response time | Timeframe for acknowledging/processing requests | TODO |
| Account deletion completion time | Timeframe for completion or explanation of retained data | TODO |
| Account profile retention | Exact retention/anonymization rule after request | TODO |
| Session retention | Exact session/audit retention window | TODO |
| Invite retention | Exact invite record retention window | TODO |
| Approval/work-order retention | Exact operational evidence retention window | TODO |
| Audit log retention | Exact retention and anonymization/deletion exception | TODO |
| Device token retention | Exact rule if notifications are enabled | TODO |
| Server log retention | Exact backend/logging retention window | TODO |
| Backup retention | Exact backup expiry window | TODO |
| Backend host/provider | Provider and region, if deployed | TODO |
| Database provider | Provider and region, if managed | TODO |
| Logging/monitoring provider | Provider and region, if used | TODO |
| Email provider | Provider and region, if verification/invites use email delivery | TODO |
| Push provider | Provider and region, if FCM/device tokens are enabled | TODO |
| GitHub/repository integration status | Whether enabled in release/review build and what data is processed | TODO |
| Analytics/crash reporting status | Whether any SDK/service is enabled | TODO |
| Legal/privacy review owner | Person/team who reviewed final wording | TODO |

## Validation command

Run this before using the URLs in Play Console:

```bash
python scripts/validate_public_pages_ready.py
```

Expected result before values are finalized:

```text
Public pages are not Play submission ready
```

Expected result after values are finalized:

```text
Public pages have no obvious placeholders and contain required disclosure sections.
```

## Current honest status

```text
Public pages exist.
Hosting workflow exists.
Submission-ready values are not finalized.
```

## Hard truth

A privacy page with fake values can cause more trouble than no page. Fill these fields only with real values you can defend.
