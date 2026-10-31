# Gate 3 emulator runtime recheck — 2026-11-01

## Result

**Not verified.** No current Antigravity or Codex request was approved or denied during this check. The existing AVD did not provide a stable Android UI, so no native invocation round trip is claimed.

## Existing AVD observations

- ADB listed only `emulator-5554` for the Control Tower test device. `sys.boot_completed` returned `1`, but Android framework services were intermittently unavailable.
- The installed package was `com.aixion.controltower.debug`; `pm path` returned an installed APK. `dumpsys package` listed `com.aixion.controltower.debug/com.aixion.controltower.MainActivity` in the `MAIN`/`LAUNCHER` resolver table.
- Despite that package metadata, explicit `am start -W -n com.aixion.controltower.debug/com.aixion.controltower.MainActivity` returned `Error type 3`, and `monkey -p com.aixion.controltower.debug 1` reported no activities. The UI screenshot showed a black screen / “Pixel is starting…” rather than the app.
- Android’s DropBox ANR evidence included a `system_server_anr` at 2026-11-01 02:01 IST. Its CPU-pressure sample reported `some avg10=66.88`, `avg60=74.37`, and `avg300=49.94`; the sample listed `system_server` at 88% CPU and the Control Tower process at 55% CPU. This is a correlation from one ANR sample, not proof that the app caused the framework failure.
- Subsequent `DeadSystemException` entries and repeated `system_server`/System UI ANRs were present. At later probes, `activity` or `window` service checks and `am start` failed with `Broken pipe` or service-unavailable errors. The app process was not present in a later `top` sample.

## Preservation and diagnostic actions

- No `-wipe-data`, uninstall, package-data clear, or removal of the existing AVD was run. Existing AVD `aixion-gate3-local-clean` remains on port 5554 with its `userdata-qemu.img.qcow2` intact.
- ADB root was enabled temporarily to read `/data/anr` and then returned to the normal shell identity (`uid=2000(shell)`).
- A separate AVD profile was registered under `/Volumes/TradeBot/AixionControlTower/avd-gate3-clean/` to avoid reusing the existing AVD data. Its startup stalled before ADB registration in the emulator's `filelock_create_timeout` path. The launch process was stopped before it created guest user data. Its generated metadata remains in place; it was not cleaned up.
- Other existing emulator processes were left untouched. The separate profile is not a usable replacement yet.

## Reproduction checks

Run from a machine with the Android SDK and the AVD connected:

```sh
adb devices -l
adb -s emulator-5554 shell getprop sys.boot_completed
adb -s emulator-5554 shell service check activity
adb -s emulator-5554 shell service check window
adb -s emulator-5554 shell pm path com.aixion.controltower.debug
adb -s emulator-5554 shell dumpsys package com.aixion.controltower.debug
adb -s emulator-5554 shell am start -W -n com.aixion.controltower.debug/com.aixion.controltower.MainActivity
```

A passing Gate 3 check still requires a fresh request from the current native adapter, the exact request visible in Control Tower, an explicit phone decision, and evidence that the same invocation resumes on approval or stops on denial. Android CI instrumentation does not substitute for that proof.

## Next safe action

Resolve the emulator startup/runtime instability without wiping the existing AVD, or use the Pixel when it is available. Then create a fresh harmless request through the current native adapter and capture both allow and deny outcomes against their same waiting invocations. Keep Gate 3 open until those outcomes are verified.
