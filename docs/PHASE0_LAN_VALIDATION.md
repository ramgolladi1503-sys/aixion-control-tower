# Phase 0 LAN Validation

Phase 0 does not require a public backend, domain, TLS certificate, or real ChatGPT/Codex internet callback.

The honest Phase 0 target is:

```text
laptop backend
+ Android phone on same Wi-Fi
+ connector/sample payload validation
+ Agent Work visibility
+ linked approval/operator decision path where available
```

## What Phase 0 can prove

```text
Backend runs on the laptop.
Phone can reach backend over same Wi-Fi/LAN.
Android app can authenticate and load protected screens.
Connector webhook accepts sample ChatGPT/Codex-style payloads.
AgentTask is created and visible through API.
Android Agent Work can show backend-created tasks.
Approvals can be reviewed from the phone.
```

## What Phase 0 cannot prove

```text
Real ChatGPT Custom GPT can call the backend from the internet.
Real Codex can call the backend from the internet.
Public HTTPS/TLS readiness.
Production external-agent exposure.
Play Store backend availability.
```

Do not claim these until a public backend exists.

## Network setup

Both devices must be on the same Wi-Fi network.

Find laptop LAN IP on macOS:

```bash
ipconfig getifaddr en0
```

Example:

```text
192.168.1.20
```

Use this backend URL shape:

```text
http://192.168.1.20:8000
```

The Android app normalizes this to a Retrofit-safe trailing-slash URL internally:

```text
http://192.168.1.20:8000/
```

## Start backend for LAN

From repo root:

```bash
cd backend
AIXION_PROFILE=demo \
AIXION_AUTH_ENABLED=false \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Check from laptop:

```bash
curl http://192.168.1.20:8000/health
curl http://192.168.1.20:8000/ops/readiness
```

Check from phone browser:

```text
http://192.168.1.20:8000/health
```

If phone cannot open it, fix Wi-Fi/firewall/backend binding before debugging the app. The app cannot connect if the phone browser cannot reach `/health`.

## Android app base URL

The debug build defaults to the Android emulator backend URL:

```text
http://10.0.2.2:8000/
```

That is wrong for a physical phone. For phone-to-laptop Phase 0 validation, build the debug APK with the laptop LAN backend URL:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.20:8000/
```

Then install the generated debug APK on the phone.

Hard rule:

```text
emulator -> http://10.0.2.2:8000/
physical phone -> http://YOUR_LAPTOP_LAN_IP:8000/
```

Do not use `localhost` from the phone. On the phone, `localhost` means the phone itself, not the laptop.

## Android debug validation checklist

```text
1. Run `ipconfig getifaddr en0` on Mac and copy the LAN IP.
2. Start backend with `--host 0.0.0.0 --port 8000`.
3. Open `http://LAN_IP:8000/health` from the phone browser.
4. Rebuild Android debug APK with `-PAIXION_API_BASE_URL=http://LAN_IP:8000/`.
5. Install the rebuilt APK.
6. Open the app.
7. Register/login or use intentional demo auth mode.
8. Confirm Home loads.
9. Confirm Agent Work loads.
10. Confirm Approvals loads.
11. Confirm Account logout confirmation works.
12. Confirm Home back button/back gesture shows the same logout confirmation.
```

## Phase 0 validator mode

Use the live validator in LAN mode:

```bash
cd backend
python scripts/validate_live_external_agent.py \
  --mode phase0-lan \
  --base-url http://192.168.1.20:8000 \
  --skip-webhook
```

This checks:

```text
/health
/ops/readiness
```

It intentionally does not call `/ops/external-agent-readiness`, because Phase 0 is not public-external validation.

## Phase 0 connector smoke test

After creating a connector and issuing a connector secret locally:

```bash
export AIXION_BASE_URL="http://192.168.1.20:8000"
export AIXION_OWNER_TOKEN="OWNER_OR_MAINTAINER_TOKEN_IF_AUTH_ENABLED"
export AIXION_CONNECTOR_ID="connector_id"
export AIXION_CONNECTOR_SECRET="connector_secret"

cd backend
python scripts/validate_live_external_agent.py --mode phase0-lan --provider chatgpt
python scripts/validate_live_external_agent.py --mode phase0-lan --provider codex
```

If auth is disabled for demo mode and task visibility requires an owner token, either enable auth and provide a token or validate task visibility through the Android app/API route available in your demo profile.

## Android manual validation

```text
1. Laptop and phone are on same Wi-Fi.
2. Backend is running with --host 0.0.0.0.
3. Phone browser opens /health.
4. Android app points to LAN backend URL.
5. Login/register flow works or demo auth mode is intentional.
6. Home loads backend data.
7. Agent Work loads.
8. Connector sample payload creates an AgentTask.
9. Agent Work shows the created task.
10. Linked Approval opens if created.
11. Approve/Deny/Revise works where the test data supports it.
12. Logout and Home back/gesture confirmation work.
```

## Safe Phase 0 claim

```text
Aixion works on a same-Wi-Fi Phase 0 setup with laptop backend and Android phone client. The repo can validate connector-style payloads locally/LAN and show the resulting work in the app.
```

## Unsafe Phase 0 claim

```text
ChatGPT/Codex is connected over the internet.
Aixion has production external-agent integration.
Aixion is ready for Play Store users without backend deployment.
```

Those need a deployed public backend.