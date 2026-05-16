#!/usr/bin/env python3
"""Validate public Play Store pages before submitting URLs.

This guard intentionally fails while placeholder values remain. It is not legal
review. It only prevents obvious unfinished pages from being submitted.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "docs" / "public"
REQUIRED_FILES = [
    PUBLIC_DIR / "privacy-policy.html",
    PUBLIC_DIR / "account-deletion.html",
]

BLOCKED_PATTERNS = {
    "TODO placeholder": re.compile(r"\bTODO\b", re.IGNORECASE),
    "example placeholder domain": re.compile(r"example\.com", re.IGNORECASE),
    "placeholder email": re.compile(r"support/privacy email|privacy/support email", re.IGNORECASE),
    "placeholder date": re.compile(r"replace before publication", re.IGNORECASE),
    "draft warning": re.compile(r"Publication checklist", re.IGNORECASE),
}

REQUIRED_PHRASES = {
    "privacy-policy.html": [
        "Aixion Control Tower",
        "Account deletion",
        "We do not sell user data",
        "Audit",
        "retention",
    ],
    "account-deletion.html": [
        "Aixion Control Tower",
        "Account Deletion Request",
        "Inside the app",
        "Outside the app",
        "auth.account_deletion_requested",
    ],
}


def validate_file(path: Path) -> list[str]:
    failures: list[str] = []
    if not path.exists():
        return [f"missing required file: {path.relative_to(ROOT)}"]

    content = path.read_text(encoding="utf-8")
    relative = path.relative_to(ROOT)

    for label, pattern in BLOCKED_PATTERNS.items():
        if pattern.search(content):
            failures.append(f"{relative}: unresolved {label}")

    for phrase in REQUIRED_PHRASES.get(path.name, []):
        if phrase not in content:
            failures.append(f"{relative}: missing required phrase: {phrase}")

    return failures


def main() -> int:
    failures: list[str] = []
    for path in REQUIRED_FILES:
        failures.extend(validate_file(path))

    if failures:
        print("Public pages are not Play submission ready:")
        for failure in failures:
            print(f"- {failure}")
        print("\nFix every issue before submitting public URLs to Google Play Console.")
        return 1

    print("Public pages have no obvious placeholders and contain required disclosure sections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
