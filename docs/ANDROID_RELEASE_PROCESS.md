# Android Release Process

This document defines the Android release-readiness process for the Mobile Approval Console / Aixion Control Tower MVP.

It is intentionally honest: the project can validate the release build variant and now has a release-signing scaffold, but it does **not** yet have a real committed keystore, Play Store distribution, or Play upload automation.

## Current release status

```text
Debug APK:                 supported
Release variant build:     validated by local/CI commands when run
Signing config scaffold:   supported through local properties or environment variables
Signed production APK/AAB: possible only after real keystore values are provided
Play Store / internal app: not configured yet
```

Do not claim signed release readiness until a real keystore-backed APK/AAB has been generated and smoke tested.

Do not claim Play Store readiness until signing, store metadata, privacy/compliance, production backend configuration, and physical-device release smoke testing are complete.

Privacy/data-safety drafts now live at:

```text
docs/PRIVACY_POLICY_DRAFT.md
docs/PLAY_STORE_DATA_SAFETY_DRAFT.md
```

These are planning drafts, not final legal/compliance approval.

## What release validation means today

Release validation currently proves:

1. The Android release variant compiles.
2. Release-only build configuration does not break compilation.
3. The same configured API base URL mechanism works for release builds.
4. CI or local commands can catch Android release compile failures before merge.
5. The build can optionally sign the release variant when valid local signing values are present.

Release validation currently does **not** prove:

1. A production keystore exists.
2. A signed release APK/AAB has been smoke tested.
3. Play Store upload readiness.
4. App integrity / Play App Signing enrollment.
5. Runtime smoke test on a real physical device.
6. Production backend URL correctness.
7. Production secrets or Firebase release setup.
8. Privacy policy readiness.
9. Store listing readiness.
10. Review-compliant screenshots and feature graphics.

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

## Local unsigned release variant build

Use this to prove the release variant compiles without signing values:

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

## Local signed release build

Copy the template:

```bash
cd mobile/android
cp keystore.properties.example keystore.properties
```

Generate or place your local release keystore. Example only:

```bash
keytool -genkeypair \
  -v \
  -keystore release.keystore \
  -alias aixion-release \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000
```

Edit `keystore.properties` locally:

```text
ANDROID_KEYSTORE_FILE=release.keystore
ANDROID_KEYSTORE_PASSWORD=your-local-password
ANDROID_KEY_ALIAS=aixion-release
ANDROID_KEY_PASSWORD=your-local-key-password
```

Then build:

```bash
./gradlew assembleRelease
./gradlew bundleRelease
```

Expected outputs:

```text
mobile/android/app/build/outputs/apk/release/
mobile/android/app/build/outputs/bundle/release/
```

Hard rule: never commit `keystore.properties`, `release.keystore`, `*.jks`, `*.keystore`, signing passwords, or base64 keystore dumps.

## CI release validation

CI should run without signing secrets:

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

After running without signing values:

```bash
./gradlew assembleRelease
```

Expected output location is normally:

```text
mobile/android/app/build/outputs/apk/release/
```

Depending on Android Gradle Plugin behavior and signing configuration, the release output may be unsigned or may not be installable on a normal device.

Do not distribute an unsigned release APK as a real release.

## Signed APK/AAB requirement

A real signed release needs a keystore and signing credentials.

Supported local file:

```text
mobile/android/keystore.properties
```

Supported properties / environment variables:

```text
ANDROID_KEYSTORE_FILE
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

For Play Store, prefer Android App Bundle output:

```bash
cd mobile/android
./gradlew bundleRelease
```

Expected artifact:

```text
mobile/android/app/build/outputs/bundle/release/*.aab
```

## Future CI signing implementation

The current scaffold supports signing from local properties or environment variables. A later PR can add CI artifact publishing after repository secrets exist.

CI signing should use GitHub Actions secrets such as:

```text
ANDROID_KEYSTORE_BASE64
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

The CI signing flow should support:

1. Decode keystore from secret into a temporary file.
2. Pass signing values through environment variables.
3. Run `./gradlew bundleRelease`.
4. Upload the AAB as a workflow artifact.
5. Never echo secrets.
6. Never commit the keystore.

## Play Store preflight checklist

Before any Play Store upload, complete every item below.

### Store account and app setup

- [ ] Google Play Console account is active.
- [ ] App package name is final and will not be casually changed later.
- [ ] App category is selected.
- [ ] App distribution countries are selected.
- [ ] Internal testing track is configured before production release.

### Required app metadata

- [ ] App name is final.
- [ ] Short description is written.
- [ ] Full description is written.
- [ ] Support email is configured.
- [ ] Privacy policy URL is live and accessible without login.
- [ ] App icon is ready.
- [ ] Feature graphic is ready.
- [ ] Phone screenshots are ready.
- [ ] Tablet screenshots are ready if targeting tablets.
- [ ] Demo/review notes explain that this is an approval-control console and requires backend connectivity.

Suggested metadata placeholders until final copy is approved:

```text
App name: Aixion Control Tower
Short description: Mobile approval control for AI-driven work execution.
Support email: TODO
Privacy policy URL: TODO
Feature graphic: TODO
Screenshots: TODO
```

Do not upload with TODO values.

### Privacy and compliance

- [ ] Privacy policy explains account data, approval data, audit logs, device/session data, and backend connectivity.
- [ ] Data Safety form is completed consistently with actual app behavior.
- [ ] Login/account requirement is explained in review notes.
- [ ] No demo secrets, backend tokens, invite codes, API keys, or keystores are bundled.
- [ ] App does not expose internal debug screens to public users unless deliberately scoped.
- [ ] If notifications are enabled later, notification usage is disclosed.

Use these planning docs before submission:

```text
docs/PRIVACY_POLICY_DRAFT.md
docs/PLAY_STORE_DATA_SAFETY_DRAFT.md
```

### Backend readiness

- [ ] Production or review backend URL exists.
- [ ] Backend is reachable from public internet or configured for the review flow.
- [ ] Backend uses production-like auth.
- [ ] Demo/review accounts exist if required.
- [ ] Review credentials are documented in Play Console app review notes if login is mandatory.
- [ ] Secrets are configured outside source control.
- [ ] Database persistence and recovery plan are documented.
- [ ] Audit log retention expectations are documented.

### Release artifact readiness

- [ ] `./gradlew testDebugUnitTest` passes.
- [ ] `./gradlew assembleDebug` passes.
- [ ] `./gradlew assembleRelease` passes.
- [ ] `./gradlew bundleRelease` passes with real signing values.
- [ ] Signed release APK or AAB is generated.
- [ ] Signed artifact installs or upload-validates.
- [ ] Version code is incremented.
- [ ] Version name is correct.
- [ ] Release notes are written.
- [ ] Physical-device smoke test passes against the intended backend.

### Manual product smoke checks

Use:

```text
docs/ANDROID_RELEASE_SMOKE_CHECKLIST.md
```

Required proof:

- [ ] Register/verify/login works.
- [ ] Protected screens are gated behind session.
- [ ] Home loads or shows explicit backend error.
- [ ] Approval Inbox loads or shows explicit backend error.
- [ ] Work Orders loads or shows explicit backend error.
- [ ] WorkOrder source badges are visible.
- [ ] Retry actions recover after backend returns.
- [ ] Owner role/invite/session controls work when logged in as OWNER.
- [ ] MCP mobile approval path passes if demoing MCP control.
- [ ] Audit proves request, decision, forwarding, and admin actions.

## Play Store blocked-until list

The app is blocked from honest Play Store upload until all of these are done:

```text
real keystore values provided
signed APK or AAB generated
signed artifact smoke tested
privacy policy URL
Data Safety answers
store listing metadata
review screenshots and feature graphic
production/review backend URL
review/demo login instructions
physical-device release smoke pass
release notes
versionCode/versionName bump
```

Hard truth: a compiling release variant is not a Play Store release. It is only one prerequisite.

## Recommended production release path later

The serious path is:

```text
release variant compiles
-> signed APK/AAB generated from secrets
-> install on physical phone or upload-validate in Play Console
-> auth-enabled smoke test
-> MCP approval smoke test
-> audit proof smoke test
-> tag release
-> attach artifact or upload to internal distribution
-> internal testing track
-> closed testing track if needed
-> production rollout only after review evidence
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

Safe claim today after CI or local commands pass:

```text
Android debug and release variants compile.
```

Safe claim after physical-phone smoke test:

```text
Android demo build is validated on a real phone against LAN backend.
```

Safe claim after signing exists and smoke passes:

```text
A signed Android release artifact has been generated and smoke tested.
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
| `bundleRelease` fails | App Bundle release path or signing path is not ready | Fix before Play Store upload. |
| APK cannot reach backend | Wrong API URL or LAN/network issue | Prove `/health` from phone first. |
| APK not installable | Likely unsigned/release signing issue | Use debug for demo or provide real signing values. |
| Missing signing secrets | Expected until signing values exist | Do not claim signed release. |
| Play Console rejects artifact | Signing/package/version/compliance issue | Fix the specific Play Console blocker. |
| Review cannot log in | Missing review account/backend instructions | Provide review credentials and backend availability. |
| Screenshots rejected | Wrong size/content or misleading listing | Regenerate compliant screenshots. |

## Brutal truth

A signing scaffold is not the same as a signed release. The next level is generating a real signed artifact with real keystore material, proving it against a reachable backend, and only then using it for internal testing or Play upload.
