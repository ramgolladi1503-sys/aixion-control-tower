# Android Release Smoke Checklist

This checklist is the fast human gate before a demo, review build, or Play Store preparation pass.

It does not replace `docs/RELEASE_VALIDATION.md`. It gives the Android operator a short, repeatable path with exact commands and visible checks.

## Hard rules

Do not claim the app is demo-ready unless every required item in this file passes.

Do not claim Play Store readiness from this checklist alone. Play Store readiness still needs signing, release metadata, privacy policy, screenshots, production backend configuration, and deployment evidence.

## 1. Backend validation commands

From repository root:

```bash
cd backend
python -m pytest
```

Focused backend guards:

```bash
cd backend
python -m pytest tests/test_settings.py
python -m pytest tests/test_role_permissions.py
python -m pytest tests/test_role_management.py
python -m pytest tests/test_invite_onboarding.py
python -m pytest tests/test_session_management.py
python -m pytest tests/test_work_order_provenance.py
python -m pytest tests/test_mcp_approval_demo_validation.py
python scripts/validate_mcp_approval_demo.py
```

Expected result:

```text
All listed pytest commands pass.
MCP validation script ends with MCP approval demo validation PASSED.
```

Stop if any backend command fails.

## 2. Android validation commands

From repository root:

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
./gradlew assembleRelease
```

Focused Android guards:

```bash
cd mobile/android
./gradlew testDebugUnitTest --tests '*WorkOrderRepositoryTest'
./gradlew testDebugUnitTest --tests '*SessionAdminRepositoryTest'
```

Expected result:

```text
Android JVM tests pass.
Debug build passes.
Release variant compiles.
```

This proves the release variant compiles. It does not prove signed APK readiness.

## 3. Start auth-enabled demo backend

Use auth-enabled mode for the Android smoke test:

```bash
cd backend
AIXION_PROFILE=demo AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

For emulator build:

```bash
cd mobile/android
./gradlew assembleDebug
```

For physical phone build:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

Physical-phone network proof:

```text
Open http://YOUR_LAN_IP:8000/health from the phone browser before debugging the app.
```

If `/health` fails from the phone, the app will fail too. Fix network first.

## 4. Android manual smoke path

Run these in order.

### 4.1 Auth entry gate

- [ ] Launch app fresh.
- [ ] App starts at authentication/account flow, not the protected main shell.
- [ ] Bottom navigation is not usable before a verified session exists.

Expected result:

```text
Unauthenticated user cannot silently enter Home, Approval Inbox, Work Orders, MCP Queue, or Audit.
```

### 4.2 First-owner registration

- [ ] Register first user with a valid email.
- [ ] Use a password of at least 12 characters.
- [ ] Complete email verification when enabled by the backend flow.
- [ ] Login after verification.
- [ ] Open Account.
- [ ] Confirm role shows OWNER.

Expected result:

```text
First user becomes OWNER.
Session is active.
Protected screens become available.
```

### 4.3 Protected screen load

Open each screen:

- [ ] Home.
- [ ] Approval Inbox.
- [ ] Work Orders.
- [ ] MCP Queue.
- [ ] Audit.
- [ ] Account.

Expected result:

```text
Each protected screen loads real backend data or a clear backend error state.
No authenticated screen silently falls back to mock data.
```

### 4.4 Retry behavior

Temporarily stop or block the backend, then open these screens:

- [ ] Home shows explicit backend error state.
- [ ] Home retry button is visible.
- [ ] Approval Inbox shows explicit backend error state.
- [ ] Approval Inbox retry button is visible.
- [ ] Work Orders shows explicit backend error state.
- [ ] Work Orders retry button is visible.

Restart backend and tap retry:

- [ ] Home reloads.
- [ ] Approval Inbox reloads.
- [ ] Work Orders reloads.

Expected result:

```text
Retry uses real ViewModel refresh paths.
No mock fallback appears after authenticated backend failure.
```

### 4.5 WorkOrder source badge

Create or seed WorkOrders that cover both source types:

- [ ] Manual WorkOrder created by a logged-in user.
- [ ] Agent-created WorkOrder created through scoped agent credentials.

Open Work Orders:

- [ ] Manual WorkOrder shows `Manual User Source`.
- [ ] Agent WorkOrder shows `Verified Agent Source`.
- [ ] Agent WorkOrder source label shows the agent name when available.
- [ ] Source detail shows source type/provider.
- [ ] WorkOrder still loads if audit/source metadata fetch fails, but falls back to manual defaults.

Expected result:

```text
The operator can visually tell whether a WorkOrder came from a manual user or a verified agent source.
```

### 4.6 Approval provenance

Open Approval Inbox:

- [ ] Approval card shows source/provider metadata when present.
- [ ] MCP/tool approval is distinguishable from normal approval when applicable.
- [ ] Approval decision still requires backend acceptance.

Expected result:

```text
Reviewer can see source context before approving.
Approval action is not a local-only UI state change.
```

### 4.7 Session verification and logout

- [ ] Open Account.
- [ ] Tap Verify session.
- [ ] Confirm valid session succeeds.
- [ ] Clear saved session or logout.
- [ ] Confirm protected screens require login again.
- [ ] Login again.
- [ ] Confirm protected screens work again.

Expected result:

```text
Valid token keeps access.
Cleared or invalid token removes access.
Network failure is not misclassified as invalid credentials.
```

### 4.8 Owner controls

As OWNER:

- [ ] Owner role-management panel loads.
- [ ] Owner invite-management panel loads.
- [ ] Owner session/access-management panel loads.
- [ ] Owner can create invite.
- [ ] Second user can register only with a valid invite code after bootstrap.
- [ ] Owner can view session metadata.
- [ ] Owner can expire another user's sessions.
- [ ] Owner cannot expire their own sessions through owner access-management endpoint.

Expected result:

```text
Owner controls are visible only when backend role allows them.
403/404/409 errors are shown clearly.
```

## 5. MCP mobile approval smoke path

Minimum valid flow:

- [ ] Submit mutating MCP request.
- [ ] Confirm child MCP server receives zero calls before approval.
- [ ] MCP Queue shows `WAITING_FOR_APPROVAL`.
- [ ] Open linked approval on Android.
- [ ] Approve from Android.
- [ ] Resolve gateway request.
- [ ] Confirm child MCP server receives exactly one call.
- [ ] Confirm second resolve does not forward again.
- [ ] MCP Queue refreshes to `FORWARDED`.
- [ ] Audit shows request, approval decision, and forwarding events.

Expected result:

```text
The demo proves mobile-controlled wait mode, not just a pretty approval screen.
```

## 6. Final demo go/no-go

Demo is a GO only if all are true:

- [ ] Backend full pytest passes.
- [ ] Backend focused guards pass.
- [ ] MCP validation script passes.
- [ ] Android JVM tests pass.
- [ ] Android focused repository tests pass.
- [ ] Debug build passes.
- [ ] Release variant compiles.
- [ ] Auth-first routing works.
- [ ] Registration, verification, login, and session verification work.
- [ ] Protected screens show real backend data or explicit backend errors.
- [ ] Retry buttons recover after backend returns.
- [ ] WorkOrder source badges are visible.
- [ ] Approval provenance is visible where present.
- [ ] Owner role/invite/session controls work.
- [ ] MCP queue approval path works.
- [ ] Audit proves the control story.
- [ ] CI status is either verified or explicitly stated as unverified.
- [ ] Signed APK status is explicitly stated as unavailable unless signing has been implemented.

If any box is unchecked, the honest status is:

```text
Not demo validated yet.
```

## 7. What not to claim

Do not claim:

```text
CI is green
signed APK is ready
Play Store release is ready
production deployment is ready
enterprise RBAC is complete
full enterprise session governance is complete
all Android errors are polished
```

Safe claim after this checklist passes:

```text
This is a strong demo-grade MVP with auth-first Android flow, real backend error handling, visible WorkOrder provenance, mobile approval control, MCP wait mode proof, owner access controls, and audit traceability.
```
