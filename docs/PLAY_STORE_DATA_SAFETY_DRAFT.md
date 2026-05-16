# Play Store Data Safety Draft — Aixion Control Tower

Status: Draft for Play Store planning. Not legal advice. Do not submit these answers until the final APK/AAB, permissions, SDKs, backend, privacy policy, and production/review configuration are verified.

Google Play's Data Safety form must accurately describe data collected, shared, security practices, deletion options, and purposes. This draft should be treated as a working checklist, not a final declaration.

## 1. Current draft assumptions

These assumptions must be rechecked before submission:

```text
App requires account login.
App connects to a backend service.
App stores/uses approval, work-order, audit, session, role, invite, and agent/source metadata.
App may process repository names, branch names, file paths, diffs, proposed code, validation commands, and MCP tool arguments.
App may process server/network logs through backend hosting or monitoring systems.
App does not use third-party advertising SDKs.
App does not sell user data.
App does not intentionally collect precise location, contacts, SMS, photos/videos/audio from the Android device, calendar, health, or payment card data.
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

Do not submit “No sharing” until the final backend hosts, SDKs, processors, repository integrations, email providers, notification services, and monitoring/logging providers are reviewed.

## 4. Security practices

Draft answers:

```text
Data encrypted in transit: Yes, for production/review builds only if HTTPS is enforced.
Users can request data deletion: No/TODO today; only Yes after an account deletion request path exists and is operational.
Data is not sold: Yes, if final SDK/backend review confirms no sale.
```

Hard blockers:

```text
If the release/review backend uses plain HTTP, do not claim encrypted in transit for production/review release.
Local debug LAN HTTP does not count as production security.
If users can create accounts in the app, do not claim deletion support until both in-app and outside-the-app deletion paths are handled according to Play requirements.
```

## 5. Account deletion

Draft status:

```text
Not ready.
```

Before Play submission, provide:

```text
public account deletion URL
in-app deletion/request path, if account creation remains available in app
support process for deletion requests
backend deletion/anonymization policy
clear explanation of retained audit/security records
```

If account creation is available in the app, Play may require a discoverable account deletion path. Do not leave this as a support-only afterthought.

Temporary disablement, logout, or session revocation is not account deletion.

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
email verification target
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
invite accepted_by_user_id
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
screen/API actions needed to operate the approval console
```

Purpose:

```text
app functionality
audit/security
fraud or abuse prevention
troubleshooting
analytics only if internal operational audit is considered analytics by final Play interpretation
```

### 6.5 User-generated content or operational content

Likely collected:

```text
Yes, under final Play category selection if work-order/approval text, diffs, code snippets, or MCP arguments are treated as user-generated content or app activity.
```

Examples:

```text
work-order goal/context/tasks
approval title and summary
rollback plan
required tests
repository name
branch name
file paths
diffs and proposed content metadata
agent task notes
MCP tool arguments
manual decision reason or operator-entered text
```

Purpose:

```text
app functionality
audit/security
controlled AI-assisted work execution
```

Required for app functionality:

```text
Yes, for work-order and approval control.
```

Hard rule:

```text
Do not mark Files/docs/media as No solely because the Android app does not upload local files. If approval payloads contain file contents, diffs, or documents, choose the correct Play category after final review.
```

### 6.6 App info and performance

Draft answer:

```text
Conditional.
```

Likely sources:

```text
backend server logs
runtime error logs
Android crash/diagnostics SDK, if added later
```

If crash reporting, diagnostics, or performance monitoring SDKs are added, record:

```text
crash logs
diagnostics
performance data
app version
runtime errors
```

Purpose:

```text
app functionality
security
analytics/diagnostics if used
developer communications/debugging
```

### 6.7 Device or other IDs

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

### 6.8 Location

Draft answer:

```text
No precise location.
Approximate location: No unless IP address or server logs are used to infer location, analytics, fraud prevention, or geoblocking.
```

Backend caution:

```text
IP addresses may appear in backend or hosting logs. Final Play interpretation must decide whether this is declared under Location, Device/other IDs, App activity, or not declared based on usage and policy guidance.
```

### 6.9 Files, docs, photos, videos, audio

Draft answer:

```text
No direct Android-device photo/video/audio collection.
Conditional for files/docs if approval payloads, diffs, proposed content, attachments, or repository documents are processed in a way Play classifies under this category.
```

Caution:

```text
If future approval payloads allow uploaded attachments, screenshots, documents, or raw file contents, update this answer.
```

### 6.10 Contacts

Draft answer:

```text
No
```

### 6.11 Financial info

Draft answer:

```text
No
```

### 6.12 Health and fitness

Draft answer:

```text
No
```

### 6.13 Messages, SMS, call logs

Draft answer:

```text
No SMS/MMS/call logs.
```

Caution:

```text
If email delivery is added for verification/invites, the app/backend processes email address and message delivery metadata, not SMS/call logs.
```

### 6.14 Web browsing

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
| Operational content/diffs/MCP args | Yes/Conditional by Play category | App functionality, audit/security | Yes | Must not contain secrets; category needs final review |
| Audit/security events | Yes | Security, compliance evidence, troubleshooting | Yes | Core control story |
| Session data | Yes | Auth/session management/security | Yes | Includes active/revoked state |
| Server/network logs | Conditional/likely | Security, troubleshooting | Usually yes for backend operation | Includes IP/user-agent depending hosting |
| Device token | Conditional | Push notifications | Conditional | Only if notifications enabled |
| Crash/diagnostics | Conditional | App quality/debugging | No | Only if SDK/logging added |
| Approximate location from IP | Conditional | Security/fraud/geoblocking if used | No unless used | Must be reviewed |
| Contacts | No | Not applicable | No | Recheck permissions |
| Android-device photos/videos/audio | No | Not applicable | No | Recheck permissions |
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
define anonymization/minimization behavior
define backup deletion window
define server log retention window
define notification token deletion behavior
```

## 9. Data deletion draft

Draft answer is not final.

Before submission, provide:

```text
public account deletion URL
in-app deletion/request path
operator support email or deletion form
backend deletion/anonymization policy
audit-retention exception wording
backup expiry window
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
review Play SDK Index warnings
```

## 12. Play submission blockers

Do not submit Data Safety answers until these are done:

- [ ] Final Android permissions reviewed.
- [ ] Final merged manifest reviewed.
- [ ] Final Gradle dependencies reviewed.
- [ ] Final SDK inventory reviewed, including Play SDK Index warnings.
- [ ] Final backend services/processors reviewed.
- [ ] Repository/GitHub integration status confirmed.
- [ ] Push notification status confirmed.
- [ ] Email provider status confirmed.
- [ ] Crash/analytics status confirmed.
- [ ] Server/network logging behavior confirmed.
- [ ] HTTPS production/review backend confirmed.
- [ ] Public privacy policy URL created.
- [ ] In-app privacy policy link added.
- [ ] Account deletion URL/process created.
- [ ] Retention periods finalized.
- [ ] Audit deletion/anonymization exception finalized.
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

Review backend logging and providers manually:

```text
hosting provider request logs
reverse proxy logs
application logs
database provider
email provider, if enabled
notification provider, if enabled
repository/GitHub provider, if enabled
monitoring/crash/analytics provider, if enabled
```

## 14. Suggested conservative draft selections before final review

This section is not a submission answer. It is a conservative starting point.

```text
Collects data: Yes
Data encrypted in transit: Yes only if HTTPS backend is used for review/release
Deletion request supported: No until implemented
Data sold: No, pending final SDK/provider review
Email address: Yes
Name/display name: Yes if collected
User IDs: Yes
App interactions/activity: Yes
User-generated operational content: Yes/Conditional, depending Play category mapping
Device IDs: Conditional, if FCM/device registration is enabled
Diagnostics: Conditional, if logging/crash/monitoring SDKs are enabled
Location: No, unless IP-based location is used
Contacts/SMS/Photos/Health/Payment: No, unless future features change this
```

## 15. Brutal truth

The Data Safety form is not marketing copy. If the form says one thing and the app/backend does another, the release is exposed to rejection or enforcement. Treat this draft as a checklist to remove uncertainty, not as a final answer sheet.
