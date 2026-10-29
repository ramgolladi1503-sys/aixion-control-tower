#!/usr/bin/env python3
"""Fail closed on an unsafe Android release API base URL."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from urllib.parse import urlsplit


def valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass

    if len(hostname) > 253 or hostname.endswith("."):
        return False
    labels = hostname.split(".")
    return all(
        1 <= len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    )


def is_valid_release_api_base_url(value: str) -> bool:
    if not value or any(character.isspace() or ord(character) < 0x20 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False

    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and valid_hostname(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65535)
        and not parsed.query
        and not parsed.fragment
        and value.endswith("/")
    )


def main() -> int:
    value = os.environ.get("AIXION_API_BASE_URL", "")
    if not is_valid_release_api_base_url(value):
        print(
            "Release API base URL must be a credential-free HTTPS URL ending in /.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
