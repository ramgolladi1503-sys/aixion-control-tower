#!/usr/bin/env python3
"""Fail-closed preflight for the manually dispatched production Android release."""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit


PRODUCTION_API_BASE_URL = "https://api.aixionlab.com/"
MAX_ANDROID_VERSION_CODE = 2_100_000_000
ANDROID_VERSION_NAME_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")


def validate_release_inputs(api_base_url: str, version_code: str, version_name: str) -> str | None:
    """Return a safe, actionable validation error, or None when inputs are valid."""
    if api_base_url != PRODUCTION_API_BASE_URL:
        return f"AIXION_API_BASE_URL must be exactly {PRODUCTION_API_BASE_URL} for this release workflow."

    if not re.fullmatch(r"[0-9]+", version_code):
        return f"AIXION_VERSION_CODE must be an integer from 1 to {MAX_ANDROID_VERSION_CODE}."
    try:
        parsed_code = int(version_code, 10)
    except ValueError:
        return "AIXION_VERSION_CODE must be an integer in the Android-supported range."
    if not 1 <= parsed_code <= MAX_ANDROID_VERSION_CODE:
        return f"AIXION_VERSION_CODE must be an integer from 1 to {MAX_ANDROID_VERSION_CODE}."

    if not ANDROID_VERSION_NAME_PATTERN.fullmatch(version_name):
        return "AIXION_VERSION_NAME must use MAJOR.MINOR.PATCH with an optional prerelease or build suffix."
    return None


def main() -> int:
    inputs = (
        os.environ.get("AIXION_API_BASE_URL", ""),
        os.environ.get("AIXION_VERSION_CODE", ""),
        os.environ.get("AIXION_VERSION_NAME", ""),
    )
    error = validate_release_inputs(*inputs)
    if error:
        print(error, file=sys.stderr)
        return 2

    # Keep URL parsing explicit as a defense against future changes to the canonical
    # release origin. Exact string matching above also rejects credentials, ports,
    # query strings, fragments, alternate paths, and non-HTTPS schemes.
    parsed = urlsplit(inputs[0])
    if parsed.scheme != "https" or parsed.hostname != "api.aixionlab.com" or parsed.path != "/":
        print("AIXION_API_BASE_URL failed canonical HTTPS origin validation.", file=sys.stderr)
        return 2

    print("Android release inputs passed production target and version validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
