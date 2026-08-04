#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

MAX_INPUT_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 3600


def _deny(reason: str) -> int:
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 2


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return _deny("Aixion rejected an oversized Antigravity hook payload.")
    try:
        payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return _deny(f"Aixion could not parse the Antigravity hook payload: {error}")

    session_id = (
        os.getenv("AIXION_RELAY_SESSION_ID", "").strip()
        or str(payload.get("session_id") or payload.get("sessionId") or "").strip()
    )
    if not session_id:
        return _deny("Aixion relay session id is missing.")

    executable = os.getenv("AIXION_RELAY_EXECUTABLE", "aixion-relay")
    timeout_seconds = max(
        1,
        int(os.getenv("AIXION_ANTIGRAVITY_APPROVAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    )
    try:
        completed = subprocess.run(
            [
                executable,
                "hook",
                "antigravity",
                "--session-id",
                session_id,
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _deny(f"Aixion approval hook failed closed: {error}")

    stdout = completed.stdout.strip()
    if not stdout:
        return _deny(
            "Aixion approval hook returned no decision: "
            + completed.stderr.strip()[:1000]
        )
    try:
        decision = json.loads(stdout)
    except json.JSONDecodeError as error:
        return _deny(f"Aixion returned invalid approval JSON: {error}")

    print(json.dumps(decision))
    if str(decision.get("decision", "")).lower() == "allow":
        return 0
    return completed.returncode if completed.returncode else 2


if __name__ == "__main__":
    raise SystemExit(main())
