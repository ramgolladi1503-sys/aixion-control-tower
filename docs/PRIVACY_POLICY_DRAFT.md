# Privacy Policy Draft — Aixion Control Tower

Status: Draft for product/release planning. Not legal advice. Do not publish this policy until it has been reviewed against the final app behavior, backend deployment, third-party services, permissions, SDKs, and applicable laws.

Last updated: TODO

## 1. App overview

Aixion Control Tower is a mobile approval console for reviewing, approving, rejecting, and auditing AI-assisted work execution. The app is intended to help authorized users control work orders, approval requests, agent-created tasks, MCP queue items, sessions, invites, roles, and audit records.

The app requires backend connectivity. It is not a standalone offline consumer app.

The privacy policy must be available in both places before Play Store submission:

```text
Play Console privacy policy URL field
inside the app, through an accessible Privacy Policy link or screen
```

## 2. Developer / operator information

Developer or organization name:

```text
TODO — must exactly match, or clearly identify, the entity shown in the Google Play store listing
```

App name:

```text
Aixion Control Tower
```

Privacy contact:

```text
TODO support/privacy email
```

Public privacy policy URL:

```text
TODO HTTPS URL, publicly accessible, non-geofenced, non-editable, and not a PDF
```

Account deletion request URL:

```text
Backend authenticated endpoint exists: POST /auth/account-deletion-request
Backend public info endpoint exists: GET /auth/account-deletion-info
TODO public outside-the-app HTTPS account deletion request URL for Google Play Console
TODO in-app user-facing deletion/request UI if account creation remains available in the app
```

## 3. Data we may collect

The exact collection depends on deployment configuration and enabled features. The current MVP may process the following categories.

### Account data

Examples:

```text
email address
display name
user ID
role
password credential material handled as salted password hash / authentication token material on backend
email verification state
invite acceptance metadata
owner/maintainer/reviewer role metadata
account deletion request status and timestamp
```

Purpose:

```text
account creation
login/session management
role-based access control
invite acceptance
security and abuse prevention
admin access management
account deletion request processing
```

### Approval, work-order, and operational content

Examples:

```text
project IDs and names
project descriptions
work order goals, context, tasks, affected areas, required tests, rollback plans
approval titles, summaries, target branches, file change paths, diffs, and proposed content metadata
code/file paths, branch names, repository names, validation commands, and execution summaries
manual operator notes or text entered into work orders, approvals, or agent tasks
decisions, decision timestamps, approval status, risk assessments
agent/provider/source metadata
MCP pending request metadata, tool names, arguments, and statuses
```

Purpose:

```text
displaying approval requests
reviewing proposed work
recording decisions
showing work-order provenance
proving controlled execution and traceability
coordinating AI-assisted engineering operations
```

Important: approval diffs, proposed code, work-order context, agent task text, and MCP arguments may contain user-generated operational content or confidential project information. Operators must avoid entering secrets, credentials, private keys, API tokens, or sensitive personal data into these fields.

### Audit and security data

Examples:

```text
audit event type
actor identifier
entity ID
event details
created timestamp
session metadata
revocation state
agent/connector identifiers
failed or blocked request details
approval decision history
role changes
invite creation, acceptance, revocation, and expiry records
session revocation events
account deletion request audit events
```

Purpose:

```text
security traceability
approval evidence
admin review
incident investigation
compliance and abuse prevention
accountability for controlled execution
account deletion request evidence
```

Audit records may be retained longer than normal account/session data because they are the evidence layer of the product. The final release policy must define how audit records are deleted, retained, or anonymized when an account deletion request is processed.

### Device/session data

Examples:

```text
session ID
user ID
active/revoked status
created and expiry timestamps
platform label such as android
app version when provided
device registration ID if enabled
```

Purpose:

```text
keeping users signed in
verifying active sessions
allowing owners to expire another user's access
revoking active sessions after account deletion request
security and access management
```

### Server, network, and backend logs

Depending on hosting and deployment configuration, the backend, reverse proxy, hosting provider, or monitoring system may process:

```text
IP address
request timestamp
request path or endpoint
HTTP status code
user agent or client metadata
error logs
backend health/check logs
deployment and runtime logs
```

Purpose:

```text
security monitoring
abuse prevention
troubleshooting
availability monitoring
incident investigation
```

If IP addresses are used to infer approximate location, block traffic by geography, or run analytics, the Play Store Data Safety answers and this policy must be updated before release.

### Push notification data, if enabled later

The current project includes backend/device registration scaffolding for push notifications. If push notifications are enabled in a production or review build, the app may process:

```text
FCM device token
platform
app version
notification delivery status
notification failure reason
```

Purpose:

```text
sending approval or operational notifications
tracking notification delivery state
```

If push notifications are not enabled in the released build, do not claim this category is actively collected.

### Diagnostics and app performance data

If crash reporting, analytics, logging SDKs, or monitoring services are added later, this policy must be updated before release.

Current draft assumption:

```text
No third-party analytics, crash reporting, or advertising SDK is intentionally documented for the MVP release draft.
```

Do not publish this assumption until the final Android dependency list, manifest permissions, and SDK inventory are reviewed.

## 4. Data visible to admins or team owners

Because this is an approval-control console, authorized OWNER users may see operational and security information related to other users, including:

```text
user email/display name/role
invite status
session metadata
active/revoked session state
audit events
approval/work-order actions
source/provenance metadata
account deletion request audit status
```

Purpose:

```text
access management
security review
approval governance
incident investigation
account deletion request handling
```

Final release notes and review/demo instructions should make clear that the app is intended for controlled team/enterprise use, not anonymous public social use.

## 5. Data we do not intend to collect

Based on current MVP scope, the app is not intended to collect:

```text
precise location
contacts
SMS/MMS
call logs
photos/videos/audio from the device
calendar data
health data
financial payment card data
advertising ID for ad targeting
browsing history
children's data
```

Caution: the app may process repository file paths, diffs, proposed code, and operational text entered by users or agents. That is different from accessing photos/videos/files from the user's Android device, but it can still contain sensitive business information. Do not under-disclose it in Play Store Data Safety.

If any permission, SDK, or feature later collects the data types listed above, update this policy and the Play Store Data Safety answers before release.

## 6. How we use data

We use data for:

```text
account management
authentication and session verification
role and permission enforcement
approval review and decision recording
work-order management
agent and connector provenance
MCP queue review and forwarding control
audit trail generation
owner/admin access management
security, abuse prevention, and troubleshooting
account deletion request handling
release/demo operation when explicitly configured
```

We do not use app data for:

```text
third-party advertising
selling personal data
cross-app behavioral profiling
unrelated marketing
```

## 7. Data sharing and service providers

The app sends data to the backend service configured for the deployment. The backend is necessary for account/session management, approvals, work orders, audit logs, account deletion request handling, and MCP/agent control.

Data may be processed by service providers that host or operate the backend, database, logging, deployment, notification, security, or monitoring systems on behalf of the app operator.

Possible processor categories:

```text
backend hosting provider
managed database provider
logging/monitoring provider
GitHub or repository integration provider, if enabled
Firebase Cloud Messaging, if push notifications are enabled
email delivery provider, if verification/invite email delivery is enabled
```

Data may be disclosed if required for:

```text
legal obligations
security investigation
fraud/abuse prevention
service operation
merger/acquisition or organizational transfer with appropriate notice
```

Current draft assumption:

```text
No data is sold.
No data is shared for advertising.
```

Do not publish this statement until final SDKs, backend providers, subprocessors, and integrations are reviewed.

## 8. International processing

Depending on the backend host, repository provider, notification provider, logging provider, and database provider, data may be processed in countries other than the user's country.

Before release, document:

```text
backend hosting region
managed database region
logging/monitoring region
notification/email provider region, if used
repository integration region/processor details, if used
```

If the app targets users in jurisdictions with specific transfer rules, legal/privacy review is required before publishing.

## 9. Security

The app and backend should use modern transport security, such as HTTPS, for production and review environments.

Sensitive data must not be committed to source control, including:

```text
API keys
backend tokens
invite codes
keystores
signing passwords
FCM server keys
raw password material
private keys
access tokens
production database files
```

Production/review builds should not use plain HTTP except for local LAN demos. A local debug backend URL is acceptable only for controlled development/testing.

Before publication, verify:

```text
release/review API base URL uses HTTPS
all user data transmitted off-device uses HTTPS or equivalent modern cryptography
no debug backend URL is shipped in a Play review/release build
no secrets are bundled in the APK/AAB
```

## 10. Data retention

Draft retention model:

| Data type | Draft retention approach | Final value needed before release |
| --- | --- | --- |
| Account profile | Retained while account exists, then disabled after deletion request and deleted or anonymized after operator processing unless retention is required | TODO exact timeline |
| Authentication/session records | Active sessions are revoked after deletion request; session records retained for a limited security/audit period | TODO exact timeline |
| Invites | Retained until accepted/revoked/expired, then retained for a limited audit/security period | TODO exact timeline |
| Approval/work-order records | Retained as operational evidence unless deleted/anonymized under retention policy | TODO exact timeline |
| Audit logs | Retained as security/control evidence and may survive account deletion in minimized/anonymized form if needed | TODO exact timeline and anonymization rule |
| Account deletion request events | Retained as proof of request receipt and processing status | TODO exact timeline |
| Device tokens | Retained while notifications/device registration remain active, then removed when disabled or account/session is deleted | TODO exact timeline |
| Server logs | Retained for troubleshooting/security for a limited period | TODO exact timeline |
| Backups | Retained until backup expiry window completes | TODO backup window |

Final retention periods must be defined before publication.

## 11. Data deletion and account deletion

Users must be able to request deletion of their account and associated personal data through both:

```text
an in-app readily discoverable deletion path, if account creation is available in the app
an outside-the-app web URL entered in Play Console
```

Implemented backend request path:

```text
POST /auth/account-deletion-request
GET /auth/account-deletion-info
```

Current backend behavior:

```text
records authenticated account deletion request
revokes active sessions for the requesting user
disables the user account
records auth.account_deletion_requested audit event
returns request status RECEIVED
```

Still required before Play submission:

```text
public outside-the-app HTTPS account deletion URL
in-app user-facing delete/request control if account creation remains available in app
operator workflow for final deletion/anonymization
published retention timelines
published audit-retention exception
```

Deletion should cover:

```text
account profile
active sessions
device tokens
invite records tied only to the account, where appropriate
personal identifiers in operational records, where deletion or anonymization is possible
```

Deletion may not immediately remove records that must be retained for:

```text
security evidence
audit integrity
legal obligations
fraud/abuse prevention
backup/recovery windows
```

If any records are retained after deletion, the final public policy must explain what is retained, why it is retained, and for how long.

Temporary account disabling, freezing, logout, or session revocation alone is not full account deletion. The current backend endpoint is a request-and-disable foundation; it does not complete all final deletion/anonymization processing by itself.

## 12. In-app disclosures and consent

If a future release collects personal or sensitive data in a way users would not reasonably expect, or collects data in the background, the app may need a prominent in-app disclosure and affirmative consent before collection begins.

Potential future triggers:

```text
background location
contacts
camera/microphone
screen recording
installed-app inventory
push notification registration beyond expected account/security use
third-party analytics/crash SDK collection beyond expected operation
```

Current draft assumption:

```text
No unexpected background personal/sensitive data collection is intentionally included in the MVP release draft.
```

Do not publish this assumption until permissions and SDKs are reviewed.

## 13. Children's privacy

This app is not intended for children and is not designed for a child-directed audience.

Draft target audience:

```text
enterprise/professional users
not directed to children
```

If the target audience changes, Play Store Families and child-data requirements must be reviewed before release.

## 14. Third-party services and SDKs

Before release, the final Android dependency list, manifest permissions, backend services, and Play SDK Index warnings must be reviewed.

For each third-party service or SDK, record:

```text
provider name
data processed
purpose
whether data is shared or processed as a service provider
whether it collects data automatically
whether it sells or uses data for advertising
retention/deletion behavior
link to provider privacy/security documentation
```

Draft SDK/service inventory table:

| Provider/service | Used now? | Data processed | Purpose | Release blocker? |
| --- | --- | --- | --- | --- |
| Backend hosting provider | TODO | Account, approval, audit, session, operational data | Core app functionality | Yes |
| Database provider | TODO | Stored backend records | Persistence | Yes |
| GitHub/repository integration | TODO if enabled | Repository names, branch names, file paths, PR metadata, execution output | Work execution / provenance | Yes if enabled |
| Firebase Cloud Messaging | Conditional | FCM token, platform, notification status | Push notifications | Yes if enabled |
| Email provider | Conditional | email address, verification/invite messages | Verification/invites | Yes if enabled |
| Logging/monitoring | Conditional | server logs, errors, diagnostics | Reliability/security | Yes if enabled |
| Analytics/crash SDK | Conditional | crash/diagnostic/app usage data | Quality/debugging | Yes if enabled |

## 15. User rights and requests

Depending on applicable law and deployment region, users may have rights to:

```text
access data
correct data
delete data
object or restrict processing
receive a copy of data
withdraw consent where processing is consent-based
complain to a regulator
```

Draft request channel:

```text
TODO privacy contact email or request portal
```

Final policy must state the actual request channel and expected response process.

## 16. Changes to this policy

We may update this privacy policy as the app changes. The updated policy should include a new effective date and should remain publicly accessible.

For material privacy changes, the operator should notify users through appropriate app, email, or release-note channels where required.

## 17. Release blockers before publishing this policy

Do not publish this policy until these are resolved:

- [ ] Developer/operator name is final and matches/identifies the Play listing entity.
- [ ] Privacy contact is final.
- [ ] Public HTTPS privacy policy URL exists and is not a PDF.
- [ ] Privacy policy link or text exists inside the app.
- [ ] Public outside-the-app account deletion request URL exists.
- [ ] In-app account deletion/request flow exists if account creation remains in app.
- [x] Backend authenticated account deletion request endpoint exists.
- [x] Backend public account deletion info endpoint exists.
- [ ] Operator deletion/anonymization workflow is defined after request receipt.
- [ ] Final Android permissions are reviewed.
- [ ] Final Android dependencies and SDKs are reviewed.
- [ ] Backend deployment architecture and hosting region are known.
- [ ] Production/review backend uses HTTPS.
- [ ] Service provider/subprocessor list is finalized.
- [ ] Push notification status is confirmed.
- [ ] Logging/analytics/crash reporting status is confirmed.
- [ ] Data retention periods are finalized.
- [ ] Backup retention window is finalized.
- [ ] Audit deletion/anonymization rules are finalized.
- [ ] Data Safety draft is made consistent with this policy.
- [ ] Legal/privacy review is complete.

## 18. Brutal truth

This draft is useful for planning, but it is not enough for Play Store release. A wrong privacy policy is worse than no polish. The final policy must match the actual APK/AAB, backend, SDKs, permissions, production/review configuration, service providers, retention rules, and deletion flow.
