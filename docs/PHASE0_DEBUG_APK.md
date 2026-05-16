# Phase 0 Debug APK

Purpose: build an installable debug APK for the Phase 0 real-device LAN test.

This APK is for local validation only. It is not a Play Store artifact.

## 1. Start the backend on your laptop

From the repo root, use the current local/demo backend command documented for the project.

Common shape:

```bash
cd backend
bash scripts/run_demo_server.sh
```

Confirm the backend is reachable from the laptop:

```bash
curl http://localhost:8000/health
```

## 2. Find your laptop LAN IP

On macOS:

```bash
ipconfig getifaddr en0
```

If that returns nothing, try:

```bash
ipconfig getifaddr en1
```

The IP should look like:

```text
192.168.x.x
10.x.x.x
172.16.x.x
```

## 3. Confirm your phone can reach the backend

Connect phone and laptop to the same Wi-Fi.

Open this on the phone browser:

```text
http://YOUR_LAN_IP:8000/health
```

Example:

```text
http://192.168.1.25:8000/health
```

If the phone cannot open it, fix Wi-Fi/firewall/backend binding before building the APK.

## 4. Build APK from GitHub Actions

Go to GitHub Actions:

```text
Actions -> Phase 0 Debug APK -> Run workflow
```

Input:

```text
api_base_url = http://YOUR_LAN_IP:8000/
artifact_name = aixion-phase0-debug-apk
```

Important:

```text
The URL must end with /.
Do not use localhost.
Do not use 127.0.0.1.
Do not use 10.0.2.2.
```

## 5. Download APK artifact

After the workflow passes:

```text
Open the workflow run
Scroll to Artifacts
Download aixion-phase0-debug-apk
Unzip it
Install the APK on your Android phone
```

## 6. Install APK on Android

Option A: copy APK to phone and tap it.

Option B: use adb:

```bash
adb install -r app-debug.apk
```

If Android blocks install, enable install from unknown sources for your browser/file manager or use adb.

## 7. Run Phase 0 LAN completion test

Use:

```text
docs/PHASE0_LAN_COMPLETION_GATE.md
```

Minimum proof:

```text
[ ] App opens on phone.
[ ] Login/register works.
[ ] App reaches laptop backend through LAN IP.
[ ] Real approvals/work orders load.
[ ] Approve works from phone.
[ ] Reject works from phone.
[ ] Audit/provenance is visible.
[ ] App restart still loads correct state.
```

## 8. Failure rules

If phone cannot reach backend:

```text
Check same Wi-Fi.
Check laptop firewall.
Check backend is not bound to localhost-only.
Check URL ends with /.
Check laptop IP did not change.
```

If app installs but backend calls fail:

```text
Rebuild APK with the correct api_base_url.
Do not reuse an APK built with an old laptop IP.
```

If approve/reject does not update backend:

```text
Phase 0 is blocked. Fix before Phase 1.
```
