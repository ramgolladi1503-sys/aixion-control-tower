# Android Firebase Messaging Setup

This project includes Firebase Cloud Messaging (FCM) client code, but does not
include a Firebase project configuration. Without the configuration, the app
must report push as unavailable and must not register a device token.

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

## Backend credentials

Configure `AIXION_FCM_PROJECT_ID` and authorize the backend's Google Application
Default Credentials to send FCM HTTP v1 messages. Use workload identity on a
supported hosted platform or a protected service-account file for local
validation. Keep service-account credentials outside the repository and never
copy them into `google-services.json`.

The production backend also requires `AIXION_PUSH_DISPATCH_ENABLED=true`. The
production profile remains blocked until the PostgreSQL-backed application
store and notification outbox are implemented and certified.
