# Privacy Policy Draft — Aixion Control Tower

Status: Draft for product/release planning. Not legal advice. Do not publish this policy until it has been reviewed against the final app behavior, backend deployment, third-party services, and applicable laws.

Last updated: TODO

## 1. App overview

Aixion Control Tower is a mobile approval console for reviewing, approving, rejecting, and auditing AI-assisted work execution. The app is intended to help authorized users control work orders, approval requests, agent-created tasks, MCP queue items, sessions, invites, and audit records.

The app requires backend connectivity. It is not a standalone offline consumer app.

## 2. Developer / operator information

Developer or organization name:

```text
TODO
```

Privacy contact:

```text
TODO support/privacy email
```

Public privacy policy URL:

```text
TODO HTTPS URL, publicly accessible, non-geofenced, not a PDF
```

Account deletion request URL:

```text
TODO HTTPS URL or support flow
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
```

Purpose:

```text
account creation
login/session management
role-based access control
invite acceptance
security and abuse prevention
```

### Approval and work execution data

Examples:

```text
project IDs and names
work order goals, context, tasks, affected areas, required tests, rollback plans
approval titles, summaries, target branches, file change paths, diffs or proposed content metadata
decisions, decision timestamps, approval status, risk assessments
agent/provider/source metadata
MCP pending request metadata and tool names
```

Purpose:

```text
displaying approval requests
reviewing proposed work
recording decisions
showing work-order provenance
proving controlled execution and traceability
```

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
```

Purpose:

```text
security traceability
approval evidence
admin review
incident investigation
compliance and abuse prevention
```

### Device/session data

Examples:

```text
session ID
user ID
active/revoked status
created and expiry timestamps
platform label such as android
app version when provided
```

Purpose:

```text
keeping users signed in
verifying active sessions
allowing owners to expire another user's access
security and access management
```

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
No third-party analytics or advertising SDK is intentionally documented for the MVP release draft.
```

Do not publish this assumption until the final Android dependency list is reviewed.

## 4. Data we do not intend to collect

Based on current MVP scope, the app is not intended to collect:

```text
precise location
contacts
SMS/MMS
call logs
photos/videos/audio
calendar data
health data
financial payment card data
advertising ID for ad targeting
browsing history
children's data
```

If any permission, SDK, or feature later collects these data types, update this policy and the Play Store Data Safety answers before release.

## 5. How we use data

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
security, abuse prevention, and troubleshooting
release/demo operation when explicitly configured
```

We do not use app data for:

```text
third-party advertising
selling personal data
cross-app behavioral profiling
unrelated marketing
```

## 6. Data sharing

The app may send data to the backend service configured for the deployment. The backend is necessary for account/session management, approvals, work orders, audit logs, and MCP/agent control.

Data may be processed by service providers that host or operate the backend, database, logging, deployment, or notification systems on behalf of the app operator.

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

Do not publish this statement until final SDKs and backend integrations are reviewed.

## 7. Security

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
```

Production/review builds should not use plain HTTP except for local LAN demos. A local debug backend URL is acceptable only for controlled development/testing.

## 8. Data retention

Draft retention model:

```text
account data: retained while the account exists or as needed for security/legal reasons
session data: retained while active and for a limited audit/security period after revocation/expiry
approval/work-order/audit records: retained as operational evidence unless deleted according to the operator's retention policy
notification tokens: retained while the device/session remains active or until revoked/removed
```

Final retention periods are TODO and must be defined before publication.

## 9. Data deletion

Users should be able to request deletion of their account and associated personal data through:

```text
TODO account deletion URL or support email
```

Deletion may not immediately remove records that must be retained for:

```text
security evidence
audit integrity
legal obligations
fraud/abuse prevention
backup/recovery windows
```

The final released app must provide a readily discoverable account-deletion path if account creation is available in the app.

## 10. Children's privacy

This app is not intended for children and is not designed for a child-directed audience.

Draft target audience:

```text
enterprise/professional users
not directed to children
```

If the target audience changes, Play Store Families and child-data requirements must be reviewed before release.

## 11. Third-party services and SDKs

Before release, the final Android dependency list and backend services must be reviewed.

Possible service categories depending on deployment:

```text
backend hosting provider
managed database provider
GitHub integration
Firebase Cloud Messaging, if push notifications are enabled
logging/monitoring provider, if added later
```

For each service, record:

```text
provider name
data processed
purpose
whether data is shared or processed as a service provider
retention/deletion behavior
link to provider privacy/security documentation
```

## 12. Changes to this policy

We may update this privacy policy as the app changes. The updated policy should include a new effective date and should remain publicly accessible.

## 13. Release blockers before publishing this policy

Do not publish this policy until these are resolved:

- [ ] Developer/operator name is final.
- [ ] Privacy contact is final.
- [ ] Public HTTPS privacy policy URL exists and is not a PDF.
- [ ] Account deletion request URL or process exists.
- [ ] Final Android permissions are reviewed.
- [ ] Final Android dependencies and SDKs are reviewed.
- [ ] Backend deployment architecture is known.
- [ ] Push notification status is confirmed.
- [ ] Logging/analytics/crash reporting status is confirmed.
- [ ] Data retention periods are finalized.
- [ ] Data Safety draft is made consistent with this policy.
- [ ] Legal/privacy review is complete.

## 14. Brutal truth

This draft is useful for planning, but it is not enough for Play Store release. A wrong privacy policy is worse than no polish. The final policy must match the actual APK, backend, SDKs, permissions, and production/review configuration.
