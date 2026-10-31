#!/usr/bin/env python3
"""Install validated release Firebase config from a protected CI secret."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any


SECRET_NAME = "ANDROID_GOOGLE_SERVICES_JSON_BASE64"
PROJECT_ID_NAME = "AIXION_FCM_PROJECT_ID"
DESTINATION = Path("mobile/android/app/google-services.json")
RELEASE_PACKAGE_NAME = "com.aixion.controltower"


def _has_release_client(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    project_info = config.get("project_info")
    project_id = project_info.get("project_id") if isinstance(project_info, dict) else None
    clients = config.get("client")
    if not isinstance(project_id, str) or not project_id.strip() or not isinstance(clients, list):
        return False

    matching_clients = 0
    for client in clients:
        if not isinstance(client, dict):
            continue
        client_info = client.get("client_info")
        android_info = (
            client_info.get("android_client_info")
            if isinstance(client_info, dict)
            else None
        )
        package_name = android_info.get("package_name") if isinstance(android_info, dict) else None
        matching_clients += package_name == RELEASE_PACKAGE_NAME
    return matching_clients == 1


def main() -> int:
    encoded = os.environ.get(SECRET_NAME, "")
    if not encoded:
        print(f"Required GitHub secret {SECRET_NAME} is missing.", file=sys.stderr)
        return 2
    expected_project_id = os.environ.get(PROJECT_ID_NAME, "").strip()
    if not expected_project_id:
        print(f"Required GitHub variable {PROJECT_ID_NAME} is missing.", file=sys.stderr)
        return 2

    try:
        raw_config = base64.b64decode(encoded, validate=True)
        config = json.loads(raw_config)
    except (ValueError, json.JSONDecodeError):
        print("Firebase Android config secret is not valid base64-encoded JSON.", file=sys.stderr)
        return 2

    project_info = config.get("project_info") if isinstance(config, dict) else None
    configured_project_id = (
        project_info.get("project_id") if isinstance(project_info, dict) else None
    )
    if not _has_release_client(config) or configured_project_id != expected_project_id:
        print(
            "Firebase Android config must match AIXION_FCM_PROJECT_ID and contain exactly one release app client.",
            file=sys.stderr,
        )
        return 2

    try:
        descriptor = os.open(DESTINATION, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Firebase Android config destination already exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw_config)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        try:
            DESTINATION.unlink()
        except FileNotFoundError:
            pass
        print("Could not write the temporary Firebase Android config.", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
