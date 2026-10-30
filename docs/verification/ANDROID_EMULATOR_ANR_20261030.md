# Android emulator ANR and approval recovery — 2026-10-30

**Result: blocked before a new approval request.** The exact pending Codex request `approval_2ce511ce6cb7` expired after the operator reported the emulator was stuck. It is not a denial and does not establish a current Codex deny path.

## Environment checked

- Draft PR: #204, unmerged
- Worktree revision before this evidence note: `865306bf938e59c87bc6047546ad3702f4200d8c`
- Android emulator: `emulator-5560`, API 35, AVD `aixion-approval-api35-local`
- App package: `com.aixion.controltower.debug`
- Isolated API: `http://127.0.0.1:8004`, bound to loopback; `/health` returned `200` and `{"status":"ok"}`
- Isolated PostgreSQL: local Unix socket under `/tmp/aixion-codex-native-e2e-20261029/socket`, database `aixion_phone_e2e`

## Observed

- The app displayed its sign-in screen. ADB input then caused Android to report repeated input-dispatch ANRs. `dumpsys activity lastanr` recorded an input-dispatch timeout at 10:32:32 local time.
- The captured ANR main-thread stack was in `MessageQueue.nativePollOnce` / `Looper.loop`, not in a visible application method. This trace does not identify an app-code deadlock; it also does not prove the emulator or app is healthy.
- A normal debug-app process stop and relaunch restored the sign-in screen. No package uninstall, app-data clear, AVD wipe, or emulator reset occurred.
- The documented runbook sample login was rejected by this isolated database. A subsequent local registration input timed out before account creation. The database still had seven users at the read-only recount; no new user was created by this attempt.
- No fresh approval request was generated, no phone decision was submitted, and no approval marker was created. The expired request remains expired.
- The local API and PostgreSQL process were left running. No external service, cloud resource, or billing setting was changed.

## Required next step

Restore a responsive emulator input path and use an owner-authorized test account that exists in this isolated database. Then create a fresh, short-lived harmless request and verify the exact phone decision and same-invocation outcome. Keep approval and denial evidence separate; a timeout or missed tap is not a denial.

This is emulator recovery evidence only. It does not close PostgreSQL production cutover, hosted HTTPS/FCM delivery, current native adapter certification, or release readiness.
