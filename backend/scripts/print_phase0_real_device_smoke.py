from __future__ import annotations

import argparse
import ipaddress
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Phase0RealDeviceSmokePlan:
    lan_ip: str
    port: int = 8000

    @property
    def base_url(self) -> str:
        return f"http://{self.lan_ip}:{self.port}"

    @property
    def android_base_url(self) -> str:
        return f"{self.base_url}/"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    @property
    def readiness_url(self) -> str:
        return f"{self.base_url}/ops/readiness"

    def backend_start_command(self) -> str:
        return "\n".join(
            [
                "cd backend",
                "AIXION_PROFILE=demo \\",
                "AIXION_AUTH_ENABLED=false \\",
                "uvicorn app.main:app --host 0.0.0.0 --port 8000",
            ]
        )

    def laptop_curl_command(self) -> str:
        return "\n".join(
            [
                f"curl {self.health_url}",
                f"curl {self.readiness_url}",
            ]
        )

    def android_build_command(self) -> str:
        return "\n".join(
            [
                "cd mobile/android",
                f"./gradlew assembleDebug -PAIXION_API_BASE_URL={self.android_base_url}",
            ]
        )

    def validator_command(self) -> str:
        return "\n".join(
            [
                "cd backend",
                "python scripts/validate_live_external_agent.py \\",
                "  --mode phase0-lan \\",
                f"  --base-url {self.base_url} \\",
                "  --skip-webhook",
            ]
        )

    def render_markdown(self) -> str:
        return f"""# Phase 0 Real-Device Smoke Plan

LAN IP: `{self.lan_ip}`
Backend base URL: `{self.base_url}`
Android base URL: `{self.android_base_url}`

## 1. Start laptop backend

```bash
{self.backend_start_command()}
```

## 2. Confirm backend from laptop

```bash
{self.laptop_curl_command()}
```

## 3. Confirm backend from phone browser

Open this URL on the Android phone while it is on the same Wi-Fi:

```text
{self.health_url}
```

If this does not open, stop. Fix Wi-Fi, firewall, or backend binding before debugging Android.

## 4. Build Android debug APK for the physical phone

```bash
{self.android_build_command()}
```

## 5. Run Phase 0 LAN validator

```bash
{self.validator_command()}
```

## 6. Manual app evidence checklist

```text
[ ] Phone browser opens /health
[ ] APK was rebuilt with the LAN backend URL
[ ] App opens on physical Android phone
[ ] Home loads backend data
[ ] Agent Work loads
[ ] Approvals loads
[ ] Account logout asks for confirmation
[ ] Home back button/back gesture asks for the same logout confirmation
[ ] Evidence screenshots/video are captured
```

## Safe claim after this passes

```text
Aixion Phase 0 works on a same-Wi-Fi real-device setup with laptop backend and Android phone client.
```

## Unsafe claim

```text
Aixion is connected to ChatGPT/Codex over the public internet.
```

That still needs a public HTTPS backend and real external-agent configuration.
"""


def normalize_lan_ip(raw_lan_ip: str) -> str:
    candidate = raw_lan_ip.strip()
    if candidate.startswith("http://"):
        candidate = candidate.removeprefix("http://")
    if candidate.startswith("https://"):
        candidate = candidate.removeprefix("https://")
    candidate = candidate.strip().strip("/")
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]

    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError("Pass a plain LAN IP such as 192.168.1.20.") from exc

    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError("Use the laptop LAN IP, not localhost, loopback, or an invalid network address.")
    if str(ip) == "10.0.2.2":
        raise ValueError("10.0.2.2 is the Android emulator host alias. Use your laptop LAN IP for a physical phone.")
    if not ip.is_private:
        raise ValueError("Phase 0 real-device validation expects a private LAN IP, for example 192.168.x.x, 10.x.x.x, or 172.16-31.x.x.")
    return str(ip)


def build_plan(raw_lan_ip: str, port: int = 8000) -> Phase0RealDeviceSmokePlan:
    if port <= 0 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return Phase0RealDeviceSmokePlan(lan_ip=normalize_lan_ip(raw_lan_ip), port=port)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a Phase 0 real-device LAN smoke validation plan.")
    parser.add_argument("--lan-ip", required=True, help="Laptop LAN IP, for example 192.168.1.20. Do not pass localhost or 10.0.2.2 for a physical phone.")
    parser.add_argument("--port", type=int, default=8000, help="Backend port. Defaults to 8000.")
    args = parser.parse_args()

    try:
        plan = build_plan(args.lan_ip, args.port)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(plan.render_markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
