# Android Debug APK from GitHub Actions

Use this workflow when you want a downloadable debug APK artifact without building locally.

Workflow:

```text
Android Debug APK
```

File:

```text
.github/workflows/android-debug-apk.yml
```

## Manual run

Open GitHub:

```text
Actions -> Android Debug APK -> Run workflow
```

Inputs:

```text
api_base_url: backend URL baked into the debug APK
artifact_name: uploaded artifact name
```

For Android emulator builds:

```text
http://10.0.2.2:8000/
```

For Phase 0 physical-phone LAN validation:

```text
http://YOUR_LAPTOP_LAN_IP:8000/
```

Example:

```text
http://192.168.29.235:8000/
```

## What the workflow does

```text
checks out repo
sets up Temurin JDK 17
runs Android unit tests
builds app-debug.apk
uploads app-debug.apk as a GitHub Actions artifact
```

## Download APK

After the workflow completes:

```text
Actions -> Android Debug APK -> latest run -> Artifacts -> aixion-debug-apk
```

Download and unzip the artifact. The APK is:

```text
app-debug.apk
```

Install with adb:

```bash
adb install -r app-debug.apk
```

## Important limitation

GitHub Actions can build an APK with your LAN URL baked in, but it cannot verify that your Android phone can reach your laptop backend.

You still must validate this from the phone browser:

```text
http://YOUR_LAPTOP_LAN_IP:8000/health
```

If the phone browser cannot open `/health`, the APK will not connect either.

## Safe claim

```text
GitHub Actions can produce a debug APK artifact configured for a chosen backend URL.
```

## Unsafe claim

```text
The GitHub Actions APK proves real-device Phase 0 connectivity.
```

That proof still requires installing on the phone and validating Home, Agent Work, Approvals, and logout confirmation against the running laptop backend.