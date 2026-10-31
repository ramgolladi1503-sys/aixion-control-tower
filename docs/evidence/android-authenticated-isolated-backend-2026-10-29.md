# Android authenticated isolated-backend preflight — 2026-10-29

**Result: PASS for authenticated Android-to-isolated-local-backend connectivity only.** This is not an Antigravity or Codex approval round trip, device certification, or production-readiness result.

## Environment

- PR: #204, draft and unmerged
- Repository revision: `184467d0bf31c360431c56b00adcfdd791e574b4`
- Android emulator: `emulator-5556`, Android API 35 (`google/sdk_gphone64_arm64`)
- App: `com.aixion.controltower.debug`, version `0.1.0`
- Debug APK SHA-256: `629e4458b4a806f62073409ea62fd3cdf2c52f083c30330a98fc5bbf38d609c9`
- APK app source and `app/build.gradle.kts` match the current revision's tracked files; only exFAT AppleDouble sidecars differ.
- Backend: current PR worktree, isolated local API on `127.0.0.1:8003`
- Database: isolated SQLite at `~/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`

## Observed

1. The app cold-launched to the centered sign-in screen without a reproduced ANR or fatal exception.
2. The configured local demo account signed in successfully.
3. The app Home screen displayed **Backend synced** and **No approvals waiting**.
4. The isolated database returned `PRAGMA integrity_check = ok`; it had zero approval delivery claims, delivery failures, or invocation leases.
5. Because this APK's debug URL is `http://10.0.2.2:8000/` and host port 8000 belongs to an existing unrelated proxy, a temporary emulator-only NAT rule scoped to this app UID and destination port was used to redirect its requests to isolated API port 8003. The host proxy and its backend were not changed.

Screenshot: [authenticated emulator Home](android-home-authenticated-isolated-backend-2026-10-29.png).

## Not established

- No approval request was created and no approve/deny action was taken.
- No Antigravity or Codex native invocation was involved.
- No provider continuation, push notification, remote-away-from-home delivery, PostgreSQL-authoritative runtime, or production behavior was verified.
- This does not close M1, M2 device acceptance, Slice 1, or any production release gate.

The emulator-only redirect is transient and not an application or host configuration change. The debug sign-in used the local test account; its credentials are not recorded here.
