# Release Validation Checklist

This checklist is the release/demo gate for the current Mobile Approval Console / Aixion Control Tower MVP.

It does **not** prove production readiness. It proves whether the current build is demo-ready without lying about CI, Android reachability, MCP forwarding, auth, audit traceability, environment configuration, or role permissions.

## Release position

Current honest position:

```text
Backend P0:                strong MVP / near P0 complete
Android MVP control flow:  strong MVP, still not release-polished
Demo-ready MVP:            yes, if this checklist passes
Production-grade product:  no
```

Do not call this production-grade until deployment, secrets, monitoring, role-based permissions, signed release handling, and session lifecycle hardening exist.

## Hard rules

1. Do not claim GitHub Actions is green unless the PR/checks page confirms it.
2. Do not claim physical-phone readiness unless a real Android device can reach the backend over LAN.
3. Do not claim authenticated Android mode unless the `Acct` login/register path works with `AIXION_AUTH_ENABLED=true`.
4. Do not claim MCP wait mode unless the child MCP server receives **zero calls before approval** and **exactly one call after approval resolve**.
5. Do not claim audit proof unless the audit trail shows request, decision, and forwarding events.
6. Do not claim signed APK readiness until a real keystore-backed signing process exists.
7. Do not claim production deployment is solved because profiles exist. Profiles reduce config confusion; they do not solve deployment.
8. Do not claim enterprise RBAC is complete. This is first-pass owner/maintainer/reviewer enforcement only.

## Evidence matrix

| Area | Required evidence | Command or proof |
| --- | --- | --- |
| Backend tests | Full backend test suite passes locally or in CI | `cd backend && python -m pytest` |
| Backend profile tests | Profile defaults and overrides are guarded | `cd backend && python -m pytest tests/test_settings.py` |
| Backend role tests | Owner/maintainer/reviewer guards are tested | `cd backend && python -m pytest tests/test_role_permissions.py` |
| Role permissions | First-pass role matrix is documented | `docs/ROLE_PERMISSIONS.md` |
| Environment profiles | Local/demo/test/production-like profiles are documented | `docs/ENVIRONMENT_PROFILES.md` |
| MCP demo script | Script exits successfully | `cd backend && python scripts/validate_mcp_approval_demo.py` |
| MCP pytest guard | Demo script is protected by pytest | `cd backend && python -m pytest tests/test_mcp_approval_demo_validation.py` |
| Android tests | Android JVM tests pass | `cd mobile/android && ./gradlew testDebugUnitTest` |
| Android session lifecycle | Expired/invalid sessions are classified and cleared | `AuthFailureTest` + Account manual check |
| Android debug build | Debug APK builds | `cd mobile/android && ./gradlew assembleDebug` |
| Android release variant | Release variant compiles | `cd mobile/android && ./gradlew assembleRelease` |
| Android release process | Release limits and signing gap are documented | `docs/ANDROID_RELEASE_PROCESS.md` |
| Auth-disabled demo | Fast local demo works without login | `AIXION_PROFILE=demo` / `AIXION_AUTH_ENABLED=false` path below |
| Auth-enabled demo | Android `Acct` login/register/session verification works and protected screens load | `AIXION_PROFILE=demo AIXION_AUTH_ENABLED=true` path below |
| Physical phone demo | Phone reaches backend over LAN | `./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/` |
| MCP approval path | Request waits, approval opens, phone approves, resolve forwards | Manual path below or runbook |
| Exactly-once forwarding | Downstream receives one call after resolve and no duplicate on second resolve | Demo script/runbook proof |
| Queue refresh | MCP Queue changes from `WAITING_FOR_APPROVAL` to `FORWARDED` | Android/manual validation |
| Audit proof | Audit includes request, approval decision, and forwarding event | `/audit` or Android Audit screen |

## 1. Backend validation

Run from repository root:

```bash
cd backend
python -m pytest
```

Expected result:

```text
all backend tests pass
```

Then run the focused profile guard:

```bash
python -m pytest tests/test_settings.py
```

Expected result:

```text
profile default and override tests pass
```

Then run the focused role guard:

```bash
python -m pytest tests/test_role_permissions.py
```

Expected result:

```text
role default and permission guard tests pass
```

Then run the focused MCP proof guard:

```bash
python -m pytest tests/test_mcp_approval_demo_validation.py
```

Expected result:

```text
test_mcp_approval_demo_validation_script_passes PASSED
```

If this fails, stop. The product proof path is not safe enough for demo.

## 2. Role permission validation

Role documentation lives at:

```text
docs/ROLE_PERMISSIONS.md
```

Supported backend roles:

```text
OWNER       agent management + all maintainer/reviewer permissions
MAINTAINER  create/execute/recover controlled work
REVIEWER    inspect evidence and make approval decisions
```

Self-registration rule:

```text
first registered user -> OWNER
later registered users -> REVIEWER
```

Known limitation:

```text
owner-only role-promotion API/UI is still future work
```

Hard rule: do not claim enterprise RBAC is complete.

## 3. Environment profile validation

Profile documentation lives at:

```text
docs/ENVIRONMENT_PROFILES.md
```

Supported backend profiles:

```text
local       auth enabled, normal local database
demo        auth disabled by default, demo database
test        auth disabled by default, test/script database
production  auth enabled, production-like behavior
```

Explicit env vars still override profile defaults:

```bash
AIXION_AUTH_ENABLED=true|false
AIXION_DB_PATH=runtime/custom.sqlite3
```

Hard rule: `production` profile is production-like configuration only. It does not mean real deployment is production-ready.

## 4. MCP approval demo validation script

Run:

```bash
cd backend
python scripts/validate_mcp_approval_demo.py
```

Expected ending:

```text
MCP approval demo validation PASSED
```

This script must prove:

```text
mutating MCP request submitted
-> gateway does not forward immediately
-> approval request is created
-> pending queue status is WAITING_FOR_APPROVAL
-> approval decision becomes APPROVED
-> approved_payload_hash exists
-> gateway resolve forwards once
-> pending queue status becomes FORWARDED
-> second resolve does not forward again
-> audit contains request, decision, and exactly-one forwarding event
```

Hard failure meanings:

| Failure | Meaning |
| --- | --- |
| Child receives call before approval | MCP wait mode is broken. |
| No approval ID created | Gateway approval creation is broken. |
| Missing `approved_payload_hash` | Integrity freeze is broken. |
| Child receives zero calls after resolve | Forwarding is broken. |
| Child receives more than one call | Idempotency is broken. Serious bug. |
| Missing audit event | Demo is not client-safe. |

## 5. Android JVM tests

Run:

```bash
cd mobile/android
./gradlew testDebugUnitTest
```

This must guard at least:

```text
MCP-backed approval detection
MCP pending queue helper behavior
approval repository truthfulness
no fake success for failed decision/resolve paths
session/auth failure classification
```

If tests fail because of fake API drift or model drift, fix that before demo. Do not wave it away as “just tests”. For this product, tests are part of the proof story.

## 6. Android debug build

Run:

```bash
cd mobile/android
./gradlew assembleDebug
```

Expected result:

```text
BUILD SUCCESSFUL
```

This proves a debug build. It does not prove signed release APK readiness.

## 7. Android release variant build

Run:

```bash
cd mobile/android
./gradlew assembleRelease
```

Expected result:

```text
BUILD SUCCESSFUL
```

This proves the release variant compiles. It does **not** prove production signing, Play Store readiness, or installability of a signed APK.

See:

```text
docs/ANDROID_RELEASE_PROCESS.md
```

Hard rule: do not claim signed release readiness until a real keystore-backed signing process exists.

## 8. Auth-disabled fast demo

Use this for the fastest backend/MCP proof.

Start backend:

```bash
cd backend
bash scripts/run_demo_server.sh
```

The script defaults to:

```text
AIXION_PROFILE=demo
AIXION_AUTH_ENABLED=false
AIXION_DB_PATH=runtime/aixion_control_tower_demo.sqlite3
```

Expected behavior:

```text
backend starts
protected API routes are usable without login as local OWNER
MCP approval path can be exercised quickly
```

This mode is valid for demoing MCP wait mode. It is not valid for proving authenticated Android behavior or real role restrictions.

## 9. Auth-enabled Android demo

Use this to prove the real Android account/session path.

Start backend:

```bash
cd backend
AIXION_PROFILE=demo AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Build Android for emulator:

```bash
cd mobile/android
./gradlew assembleDebug
```

Build Android for physical phone:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

Validation steps:

1. Open Android app.
2. Open `Acct`.
3. Register with email, display name, and a password of at least 12 characters.
4. Confirm session becomes active.
5. Tap `Verify session`.
6. Confirm session verification succeeds.
7. Open Home, Review, MCP Queue, Audit, and Acct.
8. Confirm protected screens load after login.
9. Clear saved session from `Acct`.
10. Confirm protected requests fail until login/register again.
11. Login again.
12. Confirm protected screens work again.

Expected behavior:

```text
not logged in + auth enabled -> protected calls fail
logged in + valid token -> protected calls work
invalid/expired token -> Account clears saved session and asks operator to log in again
network/backend failure -> saved token is not automatically cleared
```

That is correct. Do not treat missing-token failures as backend bugs when auth is intentionally enabled.

## 10. Physical Android phone LAN demo

Use this only when demoing on a real phone, not an emulator.

Start backend on all interfaces:

```bash
cd backend
bash scripts/run_demo_server.sh
```

Confirm from the phone browser:

```text
http://YOUR_LAN_IP:8000/health
```

Then build Android with the same LAN URL:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

Hard rules:

```text
emulator URL:       http://10.0.2.2:8000/
physical phone URL: http://YOUR_LAN_IP:8000/
wrong for phone:    http://127.0.0.1:8000/
wrong for phone:    http://10.0.2.2:8000/
```

If the phone cannot hit `/health`, the Android app will not work. Do not debug UI until network reachability is proven.

## 11. MCP approval path manual demo

The minimum valid demo path is:

```text
submit mutating MCP request
-> MCP Queue shows WAITING_FOR_APPROVAL
-> open linked approval
-> approve from phone
-> gateway resolve completes
-> MCP Queue refreshes to FORWARDED
-> Audit shows trace
```

Required proof points:

| Step | Expected result |
| --- | --- |
| Submit mutating request | `forwarded=false`, `approval_required=true`, approval ID exists |
| Before approval | Child MCP server received zero calls |
| MCP Queue | Pending row appears as `WAITING_FOR_APPROVAL` |
| Linked approval | Correct approval opens, no unrelated mock fallback |
| Phone approval | Decision succeeds only if backend accepts it |
| Resolve | Child MCP server receives exactly one call |
| Second resolve | Does not forward again |
| Queue refresh | Pending row becomes `FORWARDED` |
| Audit | Request, decision, and forwarding events exist |

If any one of these fails, the demo is not ready. Do not compensate with explanation. Fix the failing link.

## 12. Audit trail proof

Audit must prove the control story, not just show logs.

Minimum expected events:

```text
mcp.approval_requested
approval.decision
FORWARDED_AFTER_APPROVAL
```

The audit proof must answer two questions:

1. Was the MCP request blocked until approval?
2. Was the approved request forwarded exactly once?

If audit cannot answer both, the product story is incomplete.

## 13. Known failure interpretations

| Failure | Likely cause | Action |
| --- | --- | --- |
| GitHub Actions not visible | Connector/checks not inspected | Say “CI not verified”, not “green”. |
| Backend pytest fails | Backend regression | Fix before demo. |
| Settings tests fail | Profile default/override regression | Fix before demo. |
| Role tests fail | Role default/permission regression | Fix before demo. |
| Invalid profile | `AIXION_PROFILE` typo | Use `local`, `demo`, `test`, or `production`. |
| 403 forbidden after login | User role lacks permission for that action | Use the correct role or owner promotion path when implemented. |
| MCP script fails | Core product proof broken | Stop and fix. |
| Android tests fail | Model/API/fake drift or real Android logic issue | Fix before demo. |
| `assembleDebug` fails | Android compile/build issue | Fix before phone demo. |
| `assembleRelease` fails | Release variant compile issue | Fix before claiming release variant readiness. |
| Phone cannot reach backend | Wrong LAN IP, backend bound to localhost, firewall, different network | Prove `/health` from phone first. |
| Android missing token | Auth enabled but no active session | Login/register through `Acct`. |
| Android invalid/expired session | Saved bearer token is no longer usable | Account should clear saved session and ask for login. |
| Android backend/network failure during session check | Backend unreachable, not necessarily invalid token | Do not clear token automatically. Fix backend/network first. |
| Register button disabled | Password shorter than 12 characters | Use a 12+ character password. |
| MCP Queue unavailable | Backend unreachable or API failing | Fix backend/network first. |
| Linked approval opens wrong data | Mock fallback regression | Stop; this is unsafe. |
| Decision says success when backend failed | Truthfulness regression | Stop; this is unsafe. |
| Queue does not become `FORWARDED` | Resolve/refresh/status update issue | Re-run backend proof, then Android path. |
| Audit missing forwarding event | Traceability broken | Not demo-safe. |
| Signed APK unavailable | Keystore/signing process missing | Do not claim signed release readiness. |

## 14. What not to claim

Do **not** claim:

```text
CI is green
signed APK is ready
production deployment is ready
secrets are production-safe
enterprise RBAC is complete
session lifecycle is production-grade
monitoring/observability exists
all errors are polished
enterprise-ready security exists
```

Unless each claim has direct evidence.

Safe claim after this checklist passes:

```text
This is a strong demo-grade MVP proving mobile-controlled MCP approval, Android account/session flow, first-pass role enforcement, environment profile discipline, queue refresh, exactly-once forwarding, audit traceability, and Android debug/release-variant compilation.
```

Unsafe claim:

```text
This is production ready.
```

That would be dishonest.

## 15. Final go/no-go gate

Release/demo is a **GO** only if all are true:

- [ ] Backend full pytest passes.
- [ ] Backend environment profile tests pass.
- [ ] Backend role permission tests pass.
- [ ] MCP validation script passes.
- [ ] MCP validation pytest guard passes.
- [ ] Android JVM tests pass.
- [ ] Android session/auth failure classification tests pass.
- [ ] Android debug build passes.
- [ ] Android release variant build passes.
- [ ] Auth-disabled fast demo path works.
- [ ] Auth-enabled Android account/session path works.
- [ ] Physical phone can reach backend over LAN, if using real phone.
- [ ] MCP Queue shows pending request.
- [ ] Linked approval opens the correct approval.
- [ ] Phone approval resolves MCP request.
- [ ] Child MCP forwarding is exactly once.
- [ ] Queue refreshes to `FORWARDED`.
- [ ] Audit proves request, decision, and forwarding.
- [ ] CI status is either verified or explicitly stated as unverified.
- [ ] Signed APK status is explicitly stated as unavailable unless signing has been implemented.
- [ ] Production deployment status is explicitly stated as incomplete unless deployment/secrets/monitoring are implemented.
- [ ] Enterprise RBAC status is explicitly stated as incomplete unless project-scoped roles/invites/promotion UX are implemented.

If any box is unchecked, the honest status is:

```text
not release/demo validated yet
```

## Brutal truth

The product is now strong enough that bad validation discipline is the bigger risk than missing features. A sloppy demo claim will damage credibility faster than an honest “CI not verified yet.”
