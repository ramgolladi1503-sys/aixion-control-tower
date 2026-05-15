# Android Real-Device QA Gate

This gate must pass before claiming Play Store readiness. Passing backend tests and unit tests is not enough; this app is a mobile approval console, so the phone behavior is the product.

## Required validation commands

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew connectedDebugAndroidTest
./gradlew assembleDebug
```

Run backend smoke coverage before the device run:

```bash
cd backend
python -m pytest tests/test_product_regression_flow.py
python -m pytest
```

## Device matrix

Minimum real-device coverage:

| Device | Android version | Required |
|---|---:|---|
| Physical Android phone | Android 13+ | Required |
| Emulator | API 35 | Required |
| Low-end emulator profile | API 26+ | Required before public beta |

Do not use only a desktop emulator and call the app Play Store ready. That is weak QA.

## Fresh install smoke

1. Uninstall the app.
2. Install the debug/release candidate APK.
3. Launch the app.
4. Confirm the Home screen renders without a blank screen or crash.
5. Open Approval Inbox.
6. Confirm loading, empty, and populated states are readable.
7. Open Connectors.
8. Confirm templates, connector cards, webhook text, credential state, and action buttons render without clipping.

Pass criteria:

- no crash
- no blank screen
- bottom navigation usable
- text readable on normal font scale
- no action button hidden off-screen without scroll access

## Approval flow smoke

Use a backend instance with seeded or live approval data.

1. Create or seed an approval in `REQUESTED` state.
2. Open Approval Inbox.
3. Confirm the approval appears under pending count.
4. Open approval detail.
5. Approve the request.
6. Confirm state updates to approved/awaiting execution.
7. Repeat with a second request and deny it.
8. Confirm denied request does not appear as actionable.

Pass criteria:

- pending count is correct
- approve/deny calls do not crash UI
- status refresh is visible
- denied work is not shown as executable work

## Connector console smoke

1. Open Connectors.
2. Select a template.
3. Create connector from selected template.
4. Issue credential.
5. Copy credential.
6. Hide credential.
7. Copy webhook.
8. Copy setup block.
9. Apply mapper.
10. Test selected template payload.
11. Rotate credential.
12. Revoke credential.
13. Disable connector.
14. Enable connector.

Pass criteria:

- one-time credential is visible only until hidden/refreshed
- copy actions show notice feedback
- simulator result appears and is readable
- disabled/enabled state changes are visible after refresh
- raw secret is not shown after revoke or refresh

## Push notification smoke

Backend prerequisites:

- FCM server key configured for the environment
- Android build includes valid Firebase config
- device has notifications allowed

Smoke steps:

1. Register or log in on the device.
2. Trigger a new approval request from backend/external agent flow.
3. Confirm notification arrives while app is foregrounded or backgrounded.
4. Tap notification.
5. Confirm it opens the relevant approval or agent task screen.
6. Kill app and repeat notification trigger.

Pass criteria:

- notification arrives
- tap route opens relevant screen
- app does not lose auth/session state
- no duplicate notification spam for one approval event

## Offline and recovery smoke

1. Launch app online and load approvals/connectors.
2. Turn off network.
3. Reopen app.
4. Confirm app does not crash.
5. Restore network.
6. Refresh screens.

Pass criteria:

- app remains usable
- errors are readable
- refresh recovers without reinstall

## Play Store pre-release checklist

Do not submit to Play Store unless all are true:

- `./gradlew assembleDebug` or release build succeeds
- `./gradlew testDebugUnitTest` succeeds
- `./gradlew connectedDebugAndroidTest` succeeds on at least one emulator
- real physical Android smoke run completed
- approval approve/deny flow verified against backend
- connector issue/rotate/revoke flow verified against backend
- notification smoke completed or explicitly marked unavailable with reason
- no secrets are hardcoded in repo or APK build config beyond non-secret API base URL
- privacy policy text exists if any user/device/account data is collected
- screenshots match actual current UI

## Hard truth

This gate is intentionally strict. A mobile approval app that fails notification, approval, or connector smoke is not Play Store ready, even if every backend unit test passes.
