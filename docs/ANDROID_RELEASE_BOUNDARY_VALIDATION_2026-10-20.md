# Android Release Network Boundary Validation — 2026-10-20

## Result

The release variant assembled with an HTTPS API URL, its merged manifest
disables cleartext traffic, and the release pre-build gate rejects an HTTP API
URL. This verifies the local release network boundary only. It is not a signed,
installable, or production-ready release.

## Evidence

- Command: `ANDROID_HOME=/Users/madhuram/Library/Android/sdk
  ANDROID_SDK_ROOT=/Users/madhuram/Library/Android/sdk ./gradlew
  assembleRelease -PAIXION_API_BASE_URL=https://api.example.invalid/`
- Result: `BUILD SUCCESSFUL`; unsigned artifact at
  `mobile/android/app/build/outputs/apk/release/app-release-unsigned.apk`.
- Generated release `BuildConfig` contains the same non-routable HTTPS
  placeholder. No real API host was contacted.
- `aapt dump xmltree` reports
  `android:usesCleartextTraffic=(type 0x12)0x0` in the release manifest.
- Negative check:
  `./gradlew preReleaseBuild -PAIXION_API_BASE_URL=http://127.0.0.1:8000/`
  failed with the expected HTTPS-only validation error.
- SDK selection was passed through command-local environment variables. No
  `local.properties` or other machine-specific SDK setting was written.
- The build emitted SDK XML/tooling compatibility notices, Kotlin analysis API
  warnings, and an existing unused `stroke` warning in
  `ForgedLogoMark.kt`; `assembleRelease` still succeeded.

## Limits and next rollout steps

- `api.example.invalid` is deliberately non-routable; the artifact must not be
  distributed or treated as a deployment candidate.
- Release signing was not configured, so the produced APK is unsigned. Load the
  actual HTTPS host from deployment configuration and sign only in the
  protected release pipeline with its managed signing key.
- Before release, validate the real certificate chain and hostname from an
  external network, test auth/session and approval recovery against the hosted
  environment, and complete the Android permission, notification, and account
  lifecycle matrix.
- No app was installed, no cloud service was contacted, and no production
  configuration or signing material was changed.

## Revalidation — 2026-10-21

- From `mobile/android`, ran `ANDROID_HOME=/Users/madhuram/Library/Android/sdk ANDROID_SDK_ROOT=/Users/madhuram/Library/Android/sdk ./gradlew testDebugUnitTest assembleRelease -PAIXION_API_BASE_URL=https://api.example.invalid/`. Result: **BUILD SUCCESSFUL**. JUnit XML contains 26 reports, **95 tests, 0 failures, 0 errors, 0 skipped**. The release artifact remains unsigned and uses a deliberately non-routable placeholder URL.
- Negative boundary check: `./gradlew preReleaseBuild -PAIXION_API_BASE_URL=http://127.0.0.1:8000/` failed with the expected HTTPS-only validation error.
- Inspected `app/build/outputs/apk/release/app-release-unsigned.apk` with Android Build Tools 35.0.0 `aapt`; the merged manifest reports `android:usesCleartextTraffic=false`.
- `adb devices -l` reports no attached device. Instrumentation execution, physical approval UX, signed release validation, hosted auth/session behavior, and locked/background/off-LAN FCM remain unverified. This revalidation does not make the app Play Store or production ready.
