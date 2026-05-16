# Android Signed AAB CI Workflow

This document explains the manual signed Android App Bundle workflow.

Workflow:

```text
.github/workflows/android-signed-release.yml
```

## What it does

The workflow:

```text
validates signing secrets exist
decodes the release keystore into a temporary file
runs ./gradlew bundleRelease with the requested API base URL
uploads the generated .aab artifact
removes the decoded keystore at the end
```

It is intentionally manual through `workflow_dispatch`. It does not run on every PR because missing signing secrets would make normal CI fail.

## Required GitHub secrets

Configure these repository secrets before running the workflow:

```text
ANDROID_KEYSTORE_BASE64
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

## How to create ANDROID_KEYSTORE_BASE64 locally

From the directory containing the release keystore:

```bash
base64 -w 0 release.keystore
```

On macOS, use:

```bash
base64 -i release.keystore | tr -d '\n'
```

Copy the output into the GitHub secret:

```text
ANDROID_KEYSTORE_BASE64
```

Do not paste this value into chat, docs, issues, PRs, logs, or source files.

## How to run

1. Open GitHub Actions.
2. Select `Build signed Android AAB`.
3. Click `Run workflow`.
4. Enter the release/review backend URL.
5. Ensure the URL ends with `/`.
6. Run the workflow.
7. Download the uploaded AAB artifact after success.

Example backend URL:

```text
https://your-review-backend.example.com/
```

Do not use local debug URLs for Play review:

```text
http://10.0.2.2:8000/
http://127.0.0.1:8000/
http://YOUR_LAN_IP:8000/
```

## Artifact

The uploaded artifact contains:

```text
mobile/android/app/build/outputs/bundle/release/*.aab
```

Artifact retention:

```text
14 days
```

## What success proves

A successful workflow proves:

```text
signing secrets are configured
the release bundle task can use the keystore
a signed AAB artifact was produced by CI
```

## What success does not prove

It does not prove:

```text
Play Store upload is complete
Play App Signing enrollment is complete
backend is production-ready
the app passed physical-device smoke testing
privacy/data-safety pages are final
store listing metadata is ready
```

## Failure interpretations

| Failure | Meaning | Action |
| --- | --- | --- |
| Missing required GitHub secret | Signing secrets are not configured | Add all four required secrets. |
| base64 decode fails | Keystore secret is malformed | Recreate `ANDROID_KEYSTORE_BASE64`. |
| Gradle signing fails | Wrong password, alias, key password, or keystore file | Verify keystore details locally. |
| Bundle task fails | Android release build issue | Fix Android build/config. |
| No AAB uploaded | Bundle was not generated where expected | Inspect Gradle output path. |

## Hard rules

Never commit:

```text
release.keystore
*.jks
*.keystore
keystore.properties
signing.properties
ANDROID_KEYSTORE_BASE64
passwords
```

Never print signing secrets in logs.

Never claim Play Store readiness just because this workflow produces an AAB. The AAB still needs upload validation, backend review readiness, privacy/data-safety completion, and smoke testing.
