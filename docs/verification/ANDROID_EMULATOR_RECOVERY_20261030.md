# Android Emulator Recovery — 2026-10-30

## Result

The existing `aixion-approval-api35-local` AVD reaches Android boot-complete and retains its installed `com.aixion.controltower.debug` app. After allowing the guest to warm and relaunching the installed app, the sign-in screen became interactive with no ANR overlay. The intended local API on port 8004 is still absent, so login and approval verification cannot proceed. No credentials were entered, and no approval was decided or created on this AVD.

## Recovery performed

- Preserved the existing AVD at `/Volumes/TradeBotFast/AixionControlTower/avd/aixion-approval-api35-local.avd`; no `-wipe-data`, reinstall, or deletion was performed. Its `userdata-qemu.img.qcow2` remained present.
- Gracefully restarted the stuck guest, then relaunched the **same AVD and port 5560** with `-gpu auto -no-snapshot-load`.
- ADB reached `device`, and `sys.boot_completed` reached `1`. Android package, activity, and window services became available.
- The Control Tower debug package was already installed and was opened. Android produced an ANR during startup; the app process was observed in `D` state waiting in a file read of its installed `base.apk`. System services and other applications also reported startup delays/ANRs. This is evidence of a slow or stalled guest storage/startup path; it does not establish the underlying host cause.
- `adb root` was used briefly to inspect guest process state and was reverted with `adb unroot`; ADB returned to the normal `shell` user.
- On a later recovery attempt, the Android “Wait” action did not dismiss the ANR. The app was on its sign-in screen with no credentials entered or active session, so only its unresponsive process was stopped (equivalent to Android’s “Close app”); the same installed activity was relaunched without clearing app data. A cold launch remained on the splash screen and Android logged that the process failed to complete startup. After the guest had more time to warm, relaunching the same app reached the interactive sign-in screen. The package remains installed and app data was not cleared. No approval was made on this AVD.

## Backend endpoint mismatch

The sign-in screen previously displayed `Failed to connect to /10.0.2.2:8004`; after app recovery the error is not currently displayed. A read-only host check still finds no listener on `127.0.0.1:8004`. Port 8000 is a local proxy configured to connect to host port 8006, where no listener was found. Port 8003 returned health OK, but it is the separate local SQLite-backed API and is not a safe substitute for the intended test backend. No listener, proxy, API configuration, or active backend was changed.

## Remaining verification

- The AVD and app are currently responsive at sign-in; login and approval acceptance remain unverified until the intended local backend is available.
- Identify and start the intended isolated API endpoint for this app build; do not redirect it to the SQLite-only port 8003.
- The missed request `approval_2ce511ce6cb7` remains **missed/expired**, not denied. No decision or same-invocation result is claimed.
- After the correct backend is restored, create a fresh request only under the existing authorization and record the exact phone decision and resumed invocation.

This recovery does not close any production gate. The AVD and user data were preserved; no cloud, billing, DNS, deployment, database migration, or merge action occurred.
