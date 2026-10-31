# Android App Configuration

The Control Tower client is an Android app. This setup does not install a
Control Tower desktop app, menu-bar companion, or background daemon on macOS.

An agent/provider integration must submit approval requests to the Control
Tower API and receive the resulting decision. The Android app reviews and
submits decisions through that API.

## Local physical-device demo

The verified Pixel demo uses a local API on port `8001` and Android USB reverse
forwarding. This is for a phone connected to the development computer; it is
not an away-from-computer or production configuration.

From `mobile/android`:

```bash
./gradlew assembleDebug \
  -PAIXION_API_BASE_URL=http://127.0.0.1:8001/
adb reverse tcp:8001 tcp:8001
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The debug application ID is `com.aixion.controltower.debug`. The forwarding
maps the phone's `127.0.0.1:8001` to the development computer's
`127.0.0.1:8001`. Keep the API process running while using this build.

Remove the forwarding when finished:

```bash
adb reverse --remove tcp:8001
```

Do not use this HTTP URL in a release build. USB forwarding only works while
the phone is connected to the computer.

## Production Android configuration

A production build requires an externally hosted API URL with HTTPS:

```bash
./gradlew bundleRelease \
  -PAIXION_API_BASE_URL="${AIXION_RELEASE_API_URL:?set the HTTPS API URL}" \
  -PAIXION_VERSION_CODE="${AIXION_RELEASE_VERSION_CODE:?set an unused increasing Play version code}" \
  -PAIXION_VERSION_NAME="${AIXION_RELEASE_VERSION_NAME:?set MAJOR.MINOR.PATCH}"
```

The release build rejects missing, non-HTTPS, or malformed API URLs. The URL
must have a hostname, a trailing slash, and no credentials, query, or fragment.
Do not use loopback, USB reverse, emulator, or private-LAN URLs for release.

Push configuration is optional for local builds and is not enabled by merely
including the Firebase library. To build with Firebase Android configuration:

1. Obtain the app's `google-services.json` from the authorized Firebase project.
2. Place it at `mobile/android/app/google-services.json`.
3. Build with `-PAIXION_FIREBASE_CONFIG_ENABLED=true`.
4. Configure and enable the matching backend FCM project and delivery path.
5. Verify notification permission, device registration, push delivery, app
   navigation to the matching approval, and behavior when push is unavailable.

Never commit Firebase service-account credentials, signing material, or private
keys. Do not treat a successful Firebase-configured build as proof that push
delivery works.

A signed release also requires these local secret inputs:

- `ANDROID_KEYSTORE_FILE`
- `ANDROID_KEYSTORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

Supply them through the approved secret store or local ignored properties file.
Do not commit the keystore or its passwords.

## Current verification boundary

Verified locally:

- Debug APK build succeeds.
- The Pixel runs the debug app, is signed in, and displays `CONNECTED`.
- Home, Approvals, Agent Work, and Account appear in the bottom navigation.
- The app is connected to the local demo API through USB reverse forwarding.

Not established by that local demo:

- Availability when the development computer is asleep or disconnected.
- Production HTTPS API correctness or production database readiness.
- Push delivery to a backgrounded or locked phone.
- Signed release or Play distribution readiness.
- A new provider-to-phone-to-provider approval cycle.

Before a production rollout, configure and validate the hosted API, durable
production persistence and recovery, Firebase delivery if push is required,
release signing, and an end-to-end approval from each supported provider.
