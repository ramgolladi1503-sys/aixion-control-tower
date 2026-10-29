# Approval Detail Instrumentation Checkpoint

**Date:** 2026-10-29 (Asia/Kolkata)  
**Repository:** `ramgolladi1503-sys/aixion-control-tower`  
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), draft and unmerged  
**Source revision:** `8d0c720dd428da08159704b89310601ae9524b4c`

## Result

The complete `ApprovalDetailScreenTest` instrumentation class passed **50 tests, 0 failures, 0 skipped** on the Android API 35 emulator `emulator-5556` (`aixion-codex-e2e-20261029`). This rerun includes the corrected risk-badge selector that previously made one assertion ambiguous. It also covers the high-risk full-diff acknowledgement gate and approval expiry states.

Command, run from `mobile/android/`:

```sh
ANDROID_HOME=/Users/madhuram/Library/Android/sdk \
ANDROID_SDK_ROOT=/Users/madhuram/Library/Android/sdk \
./gradlew --offline \
  -I /tmp/aixion-ct-android-build-init.gradle \
  :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.aixion.controltower.feature.approvals.ApprovalDetailScreenTest \
  -PAIXION_API_BASE_URL=http://127.0.0.1:8001/
```

The Gradle init script directs generated build outputs to `/tmp` because the external exFAT worktree exposes macOS AppleDouble sidecar files. No files were deleted.

## Scope

This is Android approval-detail UI instrumentation evidence only. It does not certify a physical phone, FCM delivery, Codex Desktop or Antigravity native-session continuation, production PostgreSQL cutover, or production readiness. The Codex CLI approval and denial evidence remains limited to the separate checkpoint in `CODEX_CLI_ANDROID_E2E_20261029.md`.

At the time of this checkpoint, all exact-head checks reported for PR #204 completed successfully. The PR remains open and draft. No production UI behavior, configuration, schema, migration, cloud resource, billing, deployment, or merge state was changed.
