# Android Firebase Messaging Setup

This project includes Firebase Cloud Messaging (FCM) client code, but does not
include a Firebase project configuration in source control. Without a supplied
configuration, the app must report push as unavailable and must not register a
device token.

## Firebase Android app registration

Create or select the Firebase project used for Aixion. Register both Android
application IDs because debug and release variants use different package names:

```text
com.aixion.controltower.debug
com.aixion.controltower
```

Download the Firebase Android configuration and place it here:

```text
mobile/android/app/google-services.json
```

The configuration must include matching client entries for both package IDs.
The Google Services Gradle plugin checks the selected client against each
variant's application ID. A missing or mismatched client must fail the build.

Firebase documents `google-services.json` as containing unique, non-secret
project and app identifiers. It does not replace backend credentials. Never
put a service-account private key or backend credential in the Android app.

## Build behavior

Firebase initialization is disabled by default so local builds remain usable
without project configuration. Enable it explicitly when building a configured
Firebase variant:

```bash
cd mobile/android
./gradlew testDebugUnitTest assembleDebug \
  -PAIXION_FIREBASE_CONFIG_ENABLED=true \
  -PAIXION_API_BASE_URL=http://10.0.2.2:8000/
```

For a release build, use the hosted HTTPS API URL and provide the release
signing configuration separately:

```bash
./gradlew assembleRelease \
  -PAIXION_FIREBASE_CONFIG_ENABLED=true \
  -PAIXION_API_BASE_URL=https://<staging-or-production-api>/
```

Setting `AIXION_FIREBASE_CONFIG_ENABLED=true` without the configuration file
fails Gradle configuration with an explicit error. Leaving it false builds the
app without Firebase initialization; the Account screen reports that push is
unavailable. A successful build does not prove token registration, FCM send
permission, notification delivery, or deep-link routing. Use
`docs/FCM_REAL_DEVICE_VALIDATION.md` for the physical-device validation gate.

## Signed release workflow

The GitHub Actions workflow `Build signed Android AAB` uses the protected
GitHub Environment named `android-release` and reads its secret
`ANDROID_GOOGLE_SERVICES_JSON_BASE64`.
Set it to the single-line base64 encoding of the real `google-services.json`
that contains the `com.aixion.controltower` Android client. For example, on
macOS:

```bash
base64 -i mobile/android/app/google-services.json | tr -d '\n'
```

Copy that output directly into the GitHub secret value; do not commit the JSON
or paste its contents into an issue or chat. The workflow validates the base64,
JSON structure, project ID, and exactly one release package client without
printing the config, writes it with owner-only file permissions, enables the
existing Gradle Firebase release gate, and removes the temporary file even if
the job fails. The Google Services Gradle plugin remains responsible for its
full variant-specific validation.

The workflow also requires the four Android signing secrets documented in
`docs/ANDROID_RELEASE_PROCESS.md`. Configuring secrets only makes a signed AAB
attempt possible; it does not certify FCM delivery, API reachability, Play
Store acceptance, or production readiness.

Set the `AIXION_FCM_PROJECT_ID` variable in the same GitHub Environment to the
Firebase project ID configured for the backend. Configure required reviewers
and branch restrictions for that environment before using the workflow. The
installer rejects a Firebase Android config whose project ID does not match
this value. The backend still needs its own Google credentials and
`AIXION_FCM_PROJECT_ID` runtime setting; the Android config does not grant send
permission.

## Backend credentials

Configure `AIXION_FCM_PROJECT_ID` and authorize the backend's Google Application
Default Credentials to send FCM HTTP v1 messages. Use workload identity on a
supported hosted platform or a protected service-account file for local
validation. Keep service-account credentials outside the repository and never
copy them into `google-services.json`.

The production backend also requires `AIXION_PUSH_DISPATCH_ENABLED=true`. The
production profile remains blocked until the PostgreSQL-backed application
store and notification outbox are implemented and certified.
