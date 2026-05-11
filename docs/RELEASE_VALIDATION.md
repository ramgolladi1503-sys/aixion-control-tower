# Release Validation Checklist

This checklist is the release/demo gate for the current Mobile Approval Console / Aixion Control Tower MVP.

It does **not** prove production readiness. It proves whether the current build is demo-ready without lying about CI, Android reachability, MCP forwarding, auth, or audit traceability.

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

## Evidence matrix

| Area | Required evidence | Command or proof |
| --- | --- | --- |
| Backend tests | Full backend test suite passes locally or in CI | `cd backend && python -m pytest` |
| MCP demo script | Script exits successfully | `cd backend && python scripts/validate_mcp_approval_demo.py` |
| MCP pytest guard | Demo script is protected by pytest | `cd backend && python -m pytest tests/test_mcp_approval_demo_validation.py` |
| Android tests | Android JVM tests pass | `cd mobile/android && ./gradlew testDebugUnitTest` |
| Android build | Debug APK builds | `cd mobile/android && ./gradlew assembleDebug` |
| Auth-disabled demo | Fast local demo works without login | `AIXION_AUTH_ENABLED=false` path below |
| Auth-enabled demo | Android `Acct` login/register works and protected screens load | `AIXION_AUTH_ENABLED=true` path below |
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

Then run the focused MCP proof guard:

```bash
python -m pytest tests/test_mcp_approval_demo_validation.py
```

Expected result:

```text
test_mcp_approval_demo_validation_script_passes PASSED
```

If this fails, stop. The product proof path is not safe enough for demo.

## 2. MCP approval demo validation script

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

## 3. Android JVM tests

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
```

If tests fail because of fake API drift or model drift, fix that before demo. Do not wave it away as “just tests”. For this product, tests are part of the proof story.

## 4. Android debug build

Run:

```bash
cd mobile/android
./gradlew assembleDebug
```

Expected result:

```text
BUILD SUCCESSFUL
```

This only proves a debug build. It does **not** prove signed release APK readiness.

Do not claim release APK readiness until a real signed/release build process exists and is documented.

## 5. Auth-disabled fast demo

Use this for the fastest backend/MCP proof.

Start backend:

```bash
cd backend
AIXION_AUTH_ENABLED=false uvicorn app.main:app --reload
```

Or for LAN/physical-phone demo:

```bash
cd backend
bash scripts/run_demo_server.sh
```

Expected behavior:

```text
backend starts
protected API routes are usable without login
MCP approval path can be exercised quickly
```

This mode is valid for demoing MCP wait mode. It is not valid for proving authenticated Android behavior.

## 6. Auth-enabled Android demo

Use this to prove the real Android account/session path.

Start backend:

```bash
cd backend
AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
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
5. Open Home, Review, MCP Queue, Audit, and Acct.
6. Confirm protected screens load after login.
7. Clear saved session from `Acct`.
8. Confirm protected requests fail until login/register again.
9. Login again.
10. Confirm protected screens work again.

Expected behavior:

```text
not logged in + auth enabled -> protected calls fail
logged in + valid token -> protected calls work
cleared token -> protected calls fail again
```

That is correct. Do not treat missing-token failures as backend bugs when auth is intentionally enabled.

## 7. Physical Android phone LAN demo

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

## 8. MCP approval path manual demo

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

## 9. Audit trail proof

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

## 10. Known failure interpretations

| Failure | Likely cause | Action |
| --- | --- | --- |
| GitHub Actions not visible | Connector/checks not inspected | Say “CI not verified”, not “green”. |
| Backend pytest fails | Backend regression | Fix before demo. |
| MCP script fails | Core product proof broken | Stop and fix. |
| Android tests fail | Model/API/fake drift or real Android logic issue | Fix before demo. |
| `assembleDebug` fails | Android compile/build issue | Fix before phone demo. |
| Phone cannot reach backend | Wrong LAN IP, backend bound to localhost, firewall, different network | Prove `/health` from phone first. |
| Android missing token | Auth enabled but no active session | Login/register through `Acct`. |
| Register button disabled | Password shorter than 12 characters | Use a 12+ character password. |
| MCP Queue unavailable | Backend unreachable or API failing | Fix backend/network first. |
| Linked approval opens wrong data | Mock fallback regression | Stop; this is unsafe. |
| Decision says success when backend failed | Truthfulness regression | Stop; this is unsafe. |
| Queue does not become `FORWARDED` | Resolve/refresh/status update issue | Re-run backend proof, then Android path. |
| Audit missing forwarding event | Traceability broken | Not demo-safe. |

## 11. What not to claim

Do **not** claim:

```text
CI is green
signed APK is ready
production deployment is ready
secrets are production-safe
role-based access exists
session lifecycle is production-grade
monitoring/observability exists
all errors are polished
enterprise-ready security exists
```

Unless each claim has direct evidence.

Safe claim after this checklist passes:

```text
This is a strong demo-grade MVP proving mobile-controlled MCP approval, Android account/session flow, queue refresh, exactly-once forwarding, and audit traceability.
```

Unsafe claim:

```text
This is production ready.
```

That would be dishonest.

## 12. Final go/no-go gate

Release/demo is a **GO** only if all are true:

- [ ] Backend full pytest passes.
- [ ] MCP validation script passes.
- [ ] MCP validation pytest guard passes.
- [ ] Android JVM tests pass.
- [ ] Android debug build passes.
- [ ] Auth-disabled fast demo path works.
- [ ] Auth-enabled Android account path works.
- [ ] Physical phone can reach backend over LAN, if using real phone.
- [ ] MCP Queue shows pending request.
- [ ] Linked approval opens the correct approval.
- [ ] Phone approval resolves MCP request.
- [ ] Child MCP forwarding is exactly once.
- [ ] Queue refreshes to `FORWARDED`.
- [ ] Audit proves request, decision, and forwarding.
- [ ] CI status is either verified or explicitly stated as unverified.

If any box is unchecked, the honest status is:

```text
not release/demo validated yet
```

## Brutal truth

The product is now strong enough that bad validation discipline is the bigger risk than missing features. A sloppy demo claim will damage credibility faster than an honest “CI not verified yet.”
