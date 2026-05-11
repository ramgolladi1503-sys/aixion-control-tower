# Android Release Process

This document defines the Android release-readiness process for the Mobile Approval Console / Aixion Control Tower MVP.

It is intentionally honest: the project can validate the release build variant now, but it does **not** yet have production signing, Play Store distribution, or secret-backed release automation.

## Current release status

```text
Debug APK:                 supported
Release variant build:     validated by CI after this process is merged
Signed production APK:     not ready yet
Play Store / internal app: not configured yet
```

Do not claim signed release readiness until a real keystore and secret-backed signing flow exist.

## What release validation means today

Release validation currently proves:

1. The Android release variant compiles.
2. Release-only build configuration does not break compilation.
3. The same configured API base URL mechanism works for release builds.
4. CI catches Android release compile failures before merge.

Release validation currently does **not** prove:

1. Production signing.
2. Play Store upload readiness.
3. App integrity / Play App Signing.
4. Runtime smoke test on a real physical device.
5. Production backend URL correctness.
6. Production secrets or Firebase release setup.

## Local debug build

Use this for emulator/local development:

```bash
cd mobile/android
./gradlew assembleDebug
```

Default backend URL:

```text
http://10.0.2.2:8000/
```

That is correct for Android emulator only.

## Local release variant build

Use this to prove the release variant compiles:

```bash
cd mobile/android
./gradlew assembleRelease
```

For a release build pointed at a LAN backend:

```bash
cd mobile/android
./gradlew assembleRelease -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

The API base URL must end with `/` because Retrofit requires a trailing slash.

## CI release validation

CI should run:

```bash
cd mobile/android
./gradlew testDebugUnitTest
./gradlew assembleDebug
./gradlew assembleRelease
```

This means every PR must prove:

```text
Android JVM tests pass
Debug APK compiles
Release variant compiles
```

Hard truth: this is still not the same as a signed production release.

## Unsigned release output

After running:

```bash
./gradlew assembleRelease
```

Expected output location is normally:

```text
mobile/android/app/build/outputs/apk/release/
```

Depending on Android Gradle Plugin behavior and signing configuration, the release output may be unsigned or may not be installable on a normal device.

Do not distribute an unsigned release APK as a real release.

## Signed APK requirement

A real signed release needs a keystore and signing credentials.

Required secrets:

```text
ANDROID_KEYSTORE_BASE64
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

Required local-only files, never committed:

```text
release.keystore
keystore.properties
```

Never commit:

```text
*.jks
*.keystore
keystore.properties
signing.properties
real passwords
base64 keystore dumps
```

If any of those appear in git, treat it as a secret leak.

## Future signing implementation

When production signing is ready, add a Gradle signing config that reads from local properties or environment variables.

The signing flow should support:

1. Local developer release signing from `keystore.properties`.
2. CI release signing from GitHub Actions secrets.
3. No signing secrets in source control.
4. Clear failure when signing secrets are missing and a signed release is requested.

Expected future commands:

```bash
./gradlew assembleRelease
```

or, if using Android App Bundles:

```bash
./gradlew bundleRelease
```

## Recommended production release path later

The serious path is:

```text
release variant compiles
-> signed APK/AAB generated from secrets
-> install on physical phone
-> auth-enabled smoke test
-> MCP approval smoke test
-> audit proof smoke test
-> tag release
-> attach artifact or upload to internal distribution
```

Do not skip the physical-phone smoke test. This is a mobile approval product; a release that only builds in CI is not enough.

## Physical phone smoke test

Before calling a build demo-release-ready, verify on a real Android device:

1. Backend is running on LAN:

```bash
cd backend
bash scripts/run_demo_server.sh
```

2. Phone can reach:

```text
http://YOUR_LAN_IP:8000/health
```

3. APK is built with:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://YOUR_LAN_IP:8000/
```

4. App opens.
5. `Acct` login/register works if auth is enabled.
6. MCP Queue loads.
7. Linked approval opens correctly.
8. Approval resolves the MCP request.
9. MCP Queue refreshes to `FORWARDED`.
10. Audit shows request, decision, and forwarding events.

For now, this smoke test can use debug APKs. Signed release smoke testing comes later after signing exists.

## Versioning rule

Current Android version fields live in:

```text
mobile/android/app/build.gradle.kts
```

Current values:

```text
versionCode = 1
versionName = "0.1.0"
```

Before a real release:

1. Increment `versionCode`.
2. Update `versionName`.
3. Record the release validation result.
4. Tag the commit only after validation passes.

## Release claim policy

Safe claim today after CI passes:

```text
Android debug and release variants compile in CI.
```

Safe claim after physical-phone smoke test:

```text
Android demo build is validated on a real phone against LAN backend.
```

Unsafe claim today:

```text
Signed production APK is ready.
```

Unsafe claim today:

```text
Play Store release is ready.
```

Unsafe claim today:

```text
Production mobile deployment is solved.
```

## Failure interpretation

| Failure | Meaning | Action |
| --- | --- | --- |
| `testDebugUnitTest` fails | Android logic/test compile issue | Fix before merge. |
| `assembleDebug` fails | Debug build broken | Fix before demo. |
| `assembleRelease` fails | Release variant broken | Fix before claiming release readiness. |
| APK cannot reach backend | Wrong API URL or LAN/network issue | Prove `/health` from phone first. |
| APK not installable | Likely unsigned/release signing issue | Use debug for demo or implement signing. |
| Missing signing secrets | Expected until signing flow exists | Do not claim signed release. |

## Brutal truth

A debug APK is enough for a controlled MVP demo, but it is not release discipline. The next level is proving release compilation in CI, then adding real signing without leaking secrets.
