# Phase 0 LAN Completion Gate

Status: required before Phase 1 spend  
Applies to: local laptop backend + physical Android phone on same Wi-Fi LAN  
Purpose: prove Phase 0 is actually complete, not just merged in GitHub

## 1. Hard rule

Phase 0 is not complete until the real-device LAN gate is executed and recorded.

Merged PRs are not enough.

The product must be proven with:

```text
Laptop backend running locally
Physical Android phone connected to same Wi-Fi
Android API base URL pointed to laptop LAN IP
Real backend auth/session checks
Real approval/work-order data
Real approve/reject decision update
Real audit/provenance evidence
App restart recovery
```

## 2. Expected test setup

### Laptop

```text
Repository: aixion-control-tower
Backend: running locally
Host binding: LAN reachable, not localhost-only
Network: same Wi-Fi as Android phone
```

### Android phone

```text
Device: physical Android phone
Network: same Wi-Fi as laptop
App: debug or release-test build installed fresh
Backend URL: laptop LAN IP, not emulator localhost
```

Example backend URL shape:

```text
http://192.168.x.x:<backend-port>
```

Do not use this for Phase 0 LAN proof:

```text
http://localhost:<port>
http://127.0.0.1:<port>
http://10.0.2.2:<port>
```

Those are not proof that a physical phone can reach the laptop backend over LAN.

## 3. Pre-test checklist

```text
[ ] Laptop and phone are on the same Wi-Fi network.
[ ] Laptop firewall allows inbound traffic to backend port.
[ ] Backend is started in local/demo/test-safe mode.
[ ] Backend is reachable from laptop browser/curl.
[ ] Laptop LAN IP is known.
[ ] Android app build points to the laptop LAN IP.
[ ] App is freshly installed or app data is cleared.
[ ] Test account credentials are known or registration flow is available.
[ ] At least one path exists to create/trigger a pending approval.
[ ] Audit/provenance screen or backend event output can be checked.
```

## 4. Phase 0 completion test script

### Step 1 — Start backend on laptop

Record:

```text
Backend command used:
Backend URL:
Laptop LAN IP:
Backend port:
Backend profile/mode:
```

Pass criteria:

```text
[ ] Backend starts successfully.
[ ] Backend is bound to a LAN-reachable host/port.
[ ] Health/readiness endpoint or equivalent backend route responds.
```

### Step 2 — Confirm phone can reach backend

From phone browser, app network call, or adb-assisted check, confirm the backend is reachable.

Record:

```text
Phone network:
Backend URL opened from phone:
Result:
```

Pass criteria:

```text
[ ] Phone reaches backend using laptop LAN IP.
[ ] No localhost/emulator-only address is used.
```

### Step 3 — Fresh app launch/auth gate

Pass criteria:

```text
[ ] Fresh install opens auth screen.
[ ] Bottom navigation is hidden before authentication.
[ ] Protected screens cannot be used before login.
```

### Step 4 — Register, verify, and login

Pass criteria:

```text
[ ] User can register or use an existing test user.
[ ] Verification flow works in the configured local/demo mode.
[ ] User can login.
[ ] App validates session with backend.
[ ] Invalid/expired session is rejected if tested.
```

### Step 5 — Load real backend data

Pass criteria:

```text
[ ] Home loads from backend.
[ ] Approvals load from backend.
[ ] Work orders load from backend.
[ ] Authenticated paths do not show mock fallback data after backend failure.
```

Failure condition:

```text
If backend is stopped and the app silently shows fake approvals/work orders, Phase 0 fails.
```

### Step 6 — Trigger pending approval

Use the available local path to create a pending approval through agent, MCP, connector, or backend fixture/demo route.

Record:

```text
Pending approval source:
Approval ID/title:
Risk/source/provenance shown:
```

Pass criteria:

```text
[ ] Pending approval appears on Android.
[ ] Approval shows enough context for decision-making.
[ ] Source/provenance is visible where expected.
```

### Step 7 — Approve from phone

Pass criteria:

```text
[ ] Approve action can be performed from Android.
[ ] Backend state changes from pending/required action to approved/executing/ready state as designed.
[ ] Audit trail records the decision.
[ ] Downstream action continues only after approval.
```

### Step 8 — Reject from phone

Create or use a second pending approval.

Pass criteria:

```text
[ ] Reject action can be performed from Android.
[ ] Backend records rejection.
[ ] Rejected item does not execute later.
[ ] Audit trail records the rejection.
```

### Step 9 — Retry/error behavior

Pass criteria:

```text
[ ] Temporary backend/network failure shows explicit error.
[ ] Retry button/action is visible where expected.
[ ] Restoring backend/network and tapping retry reloads data.
[ ] App does not pretend success when backend failed.
```

### Step 10 — Restart recovery

Pass criteria:

```text
[ ] Close and reopen Android app.
[ ] Saved valid session is checked with backend.
[ ] Main screens reload from backend.
[ ] Prior approval/work-order state is consistent.
[ ] Invalid session is cleared or redirected to auth.
```

## 5. Evidence record template

Create a dated evidence note using this format in a follow-up PR or local validation log.

```text
Date:
Tester:
Laptop OS:
Android device/model:
Android version:
Wi-Fi network type:
Backend branch/commit:
Android build type:
Backend URL used:
Laptop LAN IP:

Backend startup command:
Android build/install command:

Step results:
[ ] Backend LAN reachable
[ ] Phone reached backend URL
[ ] Fresh install auth gate passed
[ ] Register/verify/login passed
[ ] Real backend data loaded
[ ] Pending approval visible on phone
[ ] Approve from phone passed
[ ] Reject from phone passed
[ ] Audit/provenance verified
[ ] Retry/error behavior verified
[ ] App restart recovery verified

Failures found:
- 

Fixes needed before Phase 1:
- 

Final decision:
[ ] Phase 0 complete
[ ] Phase 0 blocked
```

## 6. Phase 0 completion decision

Use this decision rule:

```text
If all critical checks pass:
  Phase 0 complete. Proceed to Phase 1 hosted backend planning.

If auth, LAN reachability, real backend data, approve/reject, audit/provenance, or restart recovery fails:
  Phase 0 blocked. Fix before Phase 1 spend.

If only minor UI copy or non-blocking polish fails:
  Record it as Phase 1/Phase 2 backlog, but do not block Phase 0 unless it affects trust or decision safety.
```

## 7. Phase 1 lock

Do not start Phase 1 paid infrastructure until one of these is true:

```text
[ ] Phase 0 completion evidence is recorded and accepted.
[ ] A conscious exception is recorded, explaining why Phase 1 begins despite missing Phase 0 evidence.
```

The preferred path is simple:

```text
Finish Phase 0 evidence first.
Then start PR #150 — Deploy hosted backend review environment.
```
