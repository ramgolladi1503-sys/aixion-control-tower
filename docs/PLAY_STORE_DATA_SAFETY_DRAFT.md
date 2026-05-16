# Play Store Data Safety Draft — Aixion Control Tower

Status: Draft for Play Store planning. Not legal advice. Do not submit these answers until the final APK/AAB, permissions, SDKs, backend, privacy policy, and production/review configuration are verified.

Google Play's Data Safety form must accurately describe data collected, shared, security practices, deletion options, and purposes. This draft should be treated as a working checklist, not a final declaration.

## 1. Current draft assumptions

These assumptions must be rechecked before submission:

```text
App requires account login.
App connects to a backend service.
App stores/uses approval, work-order, audit, session, role, invite, and agent/source metadata.
App does not use third-party advertising SDKs.
App does not sell user data.
App does not intentionally collect precise location, contacts, SMS, photos/videos/audio, calendar, health, or payment card data.
Push notification device tokens may be collected only if notification registration is enabled in the release build.
Crash/analytics SDKs are not assumed unless added later.
```

If any assumption changes, update this draft and `docs/PRIVACY_POLICY_DRAFT.md` before Play submission.

## 2. Data collection summary

Likely answer:

```text
Does the app collect or share user data? Yes.
```

Reason:

```text
The app requires accounts/sessions and processes approval/work-order/audit data through a backend.
```

## 3. Data sharing summary

Draft answer:

```text
Data shared: likely No for third-party sharing, unless backend/service providers are classified as sharing under the final Play Console interpretation and deployment model.
```

Operational note:

```text
Data is sent to the app operator's backend. Service providers may process data on behalf of the operator. Final Play answers must distinguish collection by the app/backend from sharing with third parties.
```

Do not submit “No sharing” until the final backend hosts, SDKs, processors, and notification services are reviewed.

## 4. Security practices

Draft answers:

```text
Data encrypted in transit: Yes, for production/review builds only if HTTPS is enforced.
Users can request data deletion: TODO, only Yes after an account deletion request path exists.
Data is not sold: Yes, if final SDK/backend review confirms no sale.
```

Hard blocker:

```text
If the release/review backend uses plain HTTP, do not claim encrypted in transit for production/review release.
Local debug LAN HTTP does not count as production security.
```

## 5. Account deletion

Draft status:

```text
Not ready.
```

Before Play submission, provide:

```text
public account deletion URL or in-app deletion flow
support process for deletion requests
clear explanation of retained audit/security records
```

If account creation is available in the app, Play may require a discoverable account deletion path. Do not leave this as a support-only afterthought.

## 6. Data types and purposes

### 6.1 Personal info — email address

Likely collected:

```text
Yes
```

Examples:

```text
user email
invite email
review/demo account email
```

Purpose:

```text
account management
authentication
security
app functionality
```

Shared:

```text
TODO, depends on backend/service providers and final Play classification
```

Required for app functionality:

```text
Yes, if account login remains required.
```

### 6.2 Personal info — name

Likely collected:

```text
Yes, if display name is used.
```

Examples:

```text
display name
operator/admin name if entered
```

Purpose:

```text
account management
operator identification
app functionality
```

### 6.3 User IDs

Likely collected:

```text
Yes
```

Examples:

```text
user ID
created_by_user_id
approved_by_user_id
session user_id
```

Purpose:

```text
authentication
role enforcement
audit traceability
security
app functionality
```

### 6.4 App activity / app interactions

Likely collected:

```text
Yes
```

Examples:

```text
approval decisions
work-order creation
MCP approval interactions
role changes
invite creation/acceptance/revocation
session revocation
audit events
```

Purpose:

```text
app functionality
audit/security
fraud or abuse prevention
analytics only if internal operational audit is considered analytics by final interpretation
```

### 6.5 App info and performance

Draft answer:

```text
No, unless crash reporting, diagnostics, or performance monitoring SDKs are added.
```

If added later, record:

```text
crash logs
diagnostics
performance data
```

Purpose:

```text
app functionality
analytics
developer communications/debugging
```

### 6.6 Device or other IDs

Likely collected only if push/device registration is enabled:

```text
Conditional
```

Examples:

```text
FCM registration token
platform
app version
session/device registration ID
```

Purpose:

```text
push notifications
security/session management
app functionality
```

Submission rule:

```text
Answer Yes only if the release build actually registers/stores device tokens or device IDs.
```

### 6.7 Files, docs, photos, videos, audio

Draft answer:

```text
No
```

Caution:

```text
If future approval payloads allow uploaded attachments or screenshots, update this answer.
```

### 6.8 Location

Draft answer:

```text
No
```

### 6.9 Contacts

Draft answer:

```text
No
```

### 6.10 Financial info

Draft answer:

```text
No
```

### 6.11 Health and fitness

Draft answer:

```text
No
```

### 6.12 Messages, SMS, call logs

Draft answer:

```text
No
```

### 6.13 Web browsing

Draft answer:

```text
No
```

## 7. Data purpose matrix

| Data category | Collected? | Purpose | Required? | Notes |
| --- | --- | --- | --- | --- |
| Email address | Yes | Account management, auth, security | Yes | Used for login, verification, invites |
| Name/display name | Yes if provided | Account profile, operator identification | Usually yes for app flow | May be optional depending final UI |
| User IDs | Yes | Auth, audit, role enforcement | Yes | Backend-generated IDs |
| Approval/work-order activity | Yes | App functionality, audit/security | Yes | Core product data |
| Audit/security events | Yes | Security, compliance evidence, troubleshooting | Yes | Core control story |
| Session data | Yes | Auth/session management/security | Yes | Includes active/revoked state |
| Device token | Conditional | Push notifications | Conditional | Only if notifications enabled |
| Crash/diagnostics | Conditional | App quality/debugging | No | Only if SDK added |
| Location | No | Not applicable | No | Recheck permissions |
| Contacts | No | Not applicable | No | Recheck permissions |
| Photos/videos/audio | No | Not applicable | No | Recheck permissions |
| Payment info | No | Not applicable | No | Recheck payment SDKs |

## 8. Retention draft

Draft retention answer:

```text
Account/session/audit/approval/work-order data is retained while needed for app functionality, audit evidence, security, troubleshooting, legal obligations, or configured retention policy.
```

TODO before submission:

```text
define actual retention periods
define account deletion behavior
define audit record retention exceptions
define backup deletion window
```

## 9. Data deletion draft

Draft answer is not final.

Before submission, provide:

```text
public account deletion URL
operator support email or deletion form
backend deletion/anonymization policy
audit-retention exception wording
```

Potential public wording after implementation:

```text
Users can request account deletion by contacting TODO or visiting TODO. Some audit/security records may be retained where required for security, legal, or abuse-prevention purposes.
```

Do not use this wording until the process exists.

## 10. Encryption in transit

Final answer can be Yes only if:

```text
production/review API base URL uses HTTPS
TLS is valid
no release build points at plain HTTP
no sensitive data is sent over insecure transport
```

Debug/demo LAN builds can use HTTP for local testing, but that must not be the Play review/release configuration.

## 11. Data sale and advertising

Draft answer:

```text
Data is not sold.
No advertising ID is used for ad targeting.
No third-party ad SDK is intentionally included.
```

Final verification:

```text
review Gradle dependencies
review manifest permissions
review SDKs
review backend processors
```

## 12. Play submission blockers

Do not submit Data Safety answers until these are done:

- [ ] Final Android permissions reviewed.
- [ ] Final Gradle dependencies reviewed.
- [ ] Final backend services/processors reviewed.
- [ ] Push notification status confirmed.
- [ ] Crash/analytics status confirmed.
- [ ] HTTPS production/review backend confirmed.
- [ ] Public privacy policy URL created.
- [ ] Account deletion URL/process created.
- [ ] Retention periods finalized.
- [ ] Legal/privacy review completed.
- [ ] Data Safety answers matched against `docs/PRIVACY_POLICY_DRAFT.md`.

## 13. Verification commands/checks

Review Android permissions:

```bash
cd mobile/android
./gradlew :app:processDebugMainManifest
```

Then inspect the merged manifest output under the app build directory.

Review dependencies:

```bash
cd mobile/android
./gradlew :app:dependencies
```

Review release build configuration:

```bash
cd mobile/android
./gradlew assembleRelease
```

For final Play release path after signing exists:

```bash
cd mobile/android
./gradlew bundleRelease
```

## 14. Brutal truth

The Data Safety form is not marketing copy. If the form says one thing and the app/backend does another, the release is exposed to rejection or enforcement. Treat this draft as a checklist to remove uncertainty, not as a final answer sheet.
