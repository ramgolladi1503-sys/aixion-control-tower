# Android Emulator Recovery — 2026-10-30

## Result

The existing `aixion-approval-api35-local` AVD reaches Android boot-complete and retains its installed `com.aixion.controltower.debug` app. After allowing the guest to warm and relaunching the installed app, the sign-in screen became interactive with no ANR overlay. The isolated local API is now running on port 8004 and the emulator can reach it. No credentials were entered, and no approval was decided or created on this AVD.

## Recovery performed

- Preserved the existing AVD at `/Volumes/TradeBotFast/AixionControlTower/avd/aixion-approval-api35-local.avd`; no `-wipe-data`, reinstall, or deletion was performed. Its `userdata-qemu.img.qcow2` remained present.
- Gracefully restarted the stuck guest, then relaunched the **same AVD and port 5560** with `-gpu auto -no-snapshot-load`.
- ADB reached `device`, and `sys.boot_completed` reached `1`. Android package, activity, and window services became available.
- The Control Tower debug package was already installed and was opened. Android produced an ANR during startup; the app process was observed in `D` state waiting in a file read of its installed `base.apk`. System services and other applications also reported startup delays/ANRs. This is evidence of a slow or stalled guest storage/startup path; it does not establish the underlying host cause.
- `adb root` was used briefly to inspect guest process state and was reverted with `adb unroot`; ADB returned to the normal `shell` user.
- On a later recovery attempt, the Android “Wait” action did not dismiss the ANR. The app was on its sign-in screen with no credentials entered or active session, so only its unresponsive process was stopped (equivalent to Android’s “Close app”); the same installed activity was relaunched without clearing app data. A cold launch remained on the splash screen and Android logged that the process failed to complete startup. After the guest had more time to warm, relaunching the same app reached the interactive sign-in screen. The package remains installed and app data was not cleared. No approval was made on this AVD.

## Local API recovery and boundary

- The app previously displayed `Failed to connect to /10.0.2.2:8004`. Restarted the API from the current PR worktree at `127.0.0.1:8004`, in the local profile, using the existing isolated PostgreSQL E2E database. Host `GET /health` returned HTTP 200; the emulator completed a TCP connection to `10.0.2.2:8004`.
- This is a local test service, not hosted HTTPS. Its outbox mode is scheduled, so the API did not start email or push dispatch loops; the temporary local-only crypto configuration is not a production secret. No real SMTP or FCM delivery was attempted.
- Port 8000 still proxies to unavailable port 8006. Port 8003 is a separate local SQLite-backed API and was left untouched. No DNS, cloud resource, production database, or app API configuration was changed.

## Remaining verification

- The AVD and app are responsive at sign-in, and the configured local API is reachable; authenticated login and phone approval acceptance remain unverified because no credentials were entered.
- The API process is local and temporary; hosted HTTPS on `api.aixionlab.com` and locked-phone push remain separate production gates.
- The missed request `approval_2ce511ce6cb7` remains **missed/expired**, not denied. No decision or same-invocation result is claimed.
- After a valid account signs in against this local API, create a fresh request only under the existing authorization and record the exact phone decision and resumed invocation.

This recovery does not close any production gate. The AVD and user data were preserved; no cloud, billing, DNS, deployment, database migration, or merge action occurred.
