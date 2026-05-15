# Play Store Readiness Notes

This document explains what is still required before distributing the Android app through Google Play.

## Current status

Aixion Control Tower is demo-ready / release-candidate software after final validation. It is not automatically Play Store-ready just because the core app works.

## Required before Play Store submission

### Android package readiness

```text
stable applicationId
release signing config
versionCode/versionName policy
release build type
minSdk/targetSdk reviewed
app icon and adaptive icon
splash screen polish
privacy policy URL
store listing copy
screenshots
```

### Backend readiness

```text
public HTTPS backend URL
production authentication enabled
production environment validation passing
persistent production database plan
backup/recovery plan
CORS/network policy reviewed
rate limits configured
logs/monitoring enabled
```

### Security and privacy readiness

```text
no private configuration values packaged into the app bundle
no hardcoded production credentials
no exposed debug endpoints
token storage reviewed
connector credential display is one-time only
privacy policy covers account data, audit logs, repository metadata, and connector metadata
user deletion/export story documented
```

### Google Play policy readiness

```text
data safety form completed
privacy policy published
permissions justified
network/security disclosure ready
no misleading autonomy claims
clear explanation that users control approvals and external integrations
```

## Internal testing path

Recommended order:

```text
local debug APK
real-device debug install
internal release APK/AAB
closed testing track
production track only after backend deployment proves stable
```

## Hard truth

The app can be technically strong and still fail Play Store readiness if the operational pieces are weak. Play Store prep is packaging, policy, privacy, backend deployment, and real device QA — not just code.

## Do not submit until

```text
backend URL is stable
release build signs correctly
login works on real device
approval flow works on real device
connector screen protects one-time credential display
privacy policy is live
Data Safety form is accurate
crash-free smoke test passes
```
