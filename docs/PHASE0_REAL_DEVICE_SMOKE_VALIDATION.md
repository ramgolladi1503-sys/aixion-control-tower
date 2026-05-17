# Phase 0 Real-Device Smoke Validation

This is the evidence path for Phase 0:

```text
Mac/laptop backend
+ Android physical phone on the same Wi-Fi
+ Android debug APK pointed to laptop LAN IP
+ backend Phase 0 validator passes
+ app screens load from backend
```

This does not prove public ChatGPT/Codex internet integration. It proves the Phase 0 real-device path.

## 1. Find the laptop LAN IP

On macOS:

```bash
ipconfig getifaddr en0
```

Example:

```text
192.168.1.20
```

Use the LAN IP from your machine in every command below.

## 2. Generate the exact smoke commands

From repo root:

```bash
cd backend
python scripts/print_phase0_real_device_smoke.py --lan-ip 192.168.1.20
```

The script prints:

```text
backend start command
laptop curl checks
phone browser health URL
Android debug APK build command
Phase 0 validator command
manual evidence checklist
safe/unsafe claims
```

Use this instead of manually typing commands from memory.

## 3. Start backend for phone access

From repo root:

```bash
cd backend
AIXION_PROFILE=demo \
AIXION_AUTH_ENABLED=false \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The important part is `--host 0.0.0.0`. If you run on `127.0.0.1`, the phone usually cannot reach it.

## 4. Confirm backend from laptop

```bash
curl http://192.168.1.20:8000/health
curl http://192.168.1.20:8000/ops/readiness
```

Expected result:

```text
/health returns status ok
/ops/readiness returns ready
```

## 5. Confirm backend from phone browser

Open this on the physical Android phone:

```text
http://192.168.1.20:8000/health
```

Hard stop rule:

```text
If the phone browser cannot open /health, do not debug Android app code yet.
Fix Wi-Fi, firewall, hotspot isolation, VPN, or backend binding first.
```

## 6. Build Android debug APK for the physical phone

The emulator URL is not valid for a physical phone:

```text
http://10.0.2.2:8000/
```

For a physical phone, use the laptop LAN IP:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.20:8000/
```

Then install the generated debug APK on the phone.

## 7. Run Phase 0 LAN validator

From repo root:

```bash
cd backend
python scripts/validate_live_external_agent.py \
  --mode phase0-lan \
  --base-url http://192.168.1.20:8000 \
  --skip-webhook
```

This intentionally checks `/ops/readiness`, not `/ops/external-agent-readiness`.

Reason:

```text
Phase 0 LAN validation = same-Wi-Fi runtime proof
public-external validation = deployed HTTPS backend proof
```

Do not mix them.

## 8. Manual app evidence checklist

Capture screenshots or screen recording for these:

```text
[ ] Phone browser opens http://LAN_IP:8000/health
[ ] APK was rebuilt with -PAIXION_API_BASE_URL=http://LAN_IP:8000/
[ ] App opens on physical Android phone
[ ] Home loads backend data
[ ] Agent Work loads
[ ] Approvals loads
[ ] Account Logout asks for confirmation
[ ] Logout No keeps the session
[ ] Logout Yes clears session and returns to Auth
[ ] Home back button/back gesture shows the same logout confirmation
```

Optional connector evidence after local connector setup:

```text
[ ] Connector sample payload creates AgentTask
[ ] Agent Work shows the created task
[ ] Linked Approval opens if created
[ ] Approve/Deny/Revise works where sample data supports it
```

## Common failures

### Phone browser cannot open `/health`

Likely causes:

```text
backend started on 127.0.0.1 instead of 0.0.0.0
phone and laptop are not on the same Wi-Fi
Mac firewall blocks inbound connections
VPN/hotspot client isolation blocks LAN traffic
wrong LAN IP copied
```

### App still cannot load after phone browser works

Likely causes:

```text
APK was not rebuilt after changing AIXION_API_BASE_URL
APK still points to http://10.0.2.2:8000/
backend URL missing port
backend process stopped
old APK is still installed
```

### Validator passes but app shows auth/session problems

Likely causes:

```text
intentional demo auth mode differs from app auth expectations
saved stale token exists on phone
wrong APK build installed
backend state was reset after login
```

## Safe claim after this passes

```text
Aixion Phase 0 works on a same-Wi-Fi real-device setup with laptop backend and Android phone client.
```

## Unsafe claim

```text
Aixion is connected to ChatGPT/Codex over the public internet.
Aixion is production external-agent ready.
Aixion is Play Store ready for real users without a deployed backend.
```

Those require public HTTPS deployment and real external-agent configuration.

## Validation for this runbook/tooling

```bash
cd backend
python -m pytest tests/test_phase0_real_device_smoke_plan.py
python -m pytest tests/test_phase0_real_device_smoke_plan.py tests/test_live_external_agent_validator.py
```

Hard truth: this PR is not about adding another feature. It is about proving the feature chain on a real phone without fooling yourself with emulator-only behavior.