#!/usr/bin/env python3
"""Verify public Play Store page URLs before submission.

This script checks that the privacy policy and account deletion pages are served
from public HTTPS URLs, return HTML, include required release phrases, and do not
contain obvious placeholders unless --allow-placeholders is explicitly passed.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import unescape
from typing import Iterable
from urllib.parse import urlparse

DEFAULT_PRIVACY_URL = (
    "https://ramgolladi1503-sys.github.io/aixion-control-tower/privacy-policy.html"
)
DEFAULT_ACCOUNT_DELETION_URL = (
    "https://ramgolladi1503-sys.github.io/aixion-control-tower/account-deletion.html"
)

USER_AGENT = "aixion-public-page-verifier/1.0"
MAX_RESPONSE_BYTES = 1_000_000

BLOCKED_PATTERNS = {
    "TODO placeholder": re.compile(r"\bTODO\b", re.IGNORECASE),
    "example placeholder domain": re.compile(r"example\.com", re.IGNORECASE),
    "placeholder support email": re.compile(
        r"support/privacy email|privacy/support email", re.IGNORECASE
    ),
    "placeholder date": re.compile(r"replace before publication", re.IGNORECASE),
    "publication checklist warning": re.compile(r"Publication checklist", re.IGNORECASE),
}

REQUIRED_PHRASES = {
    "privacy-policy.html": [
        "Aixion Control Tower",
        "Privacy Policy",
        "Account deletion",
        "We do not sell user data",
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


@dataclass(frozen=True)
class PageCheckResult:
    """Result from checking one public page URL."""

    label: str
    url: str
    status_code: int | None
    content_type: str
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def expected_page_key(url: str) -> str | None:
    """Return the expected page key based on URL path."""

    path = urlparse(url).path.rstrip("/")
    for key in REQUIRED_PHRASES:
        if path.endswith(f"/{key}") or path.endswith(key):
            return key
    return None


def validate_url_shape(url: str) -> list[str]:
    """Validate that a Play Store public page URL has a safe submission shape."""

    failures: list[str] = []
    parsed = urlparse(url)

    if parsed.scheme != "https":
        failures.append("URL must use HTTPS")
    if not parsed.netloc:
        failures.append("URL must include a host")
    if parsed.username or parsed.password:
        failures.append("URL must not include credentials")
    if parsed.fragment:
        failures.append("URL must not depend on a fragment")
    if parsed.path.lower().endswith(".pdf"):
        failures.append("Google Play policy/account deletion URL must not be a PDF")
    if expected_page_key(url) is None:
        failures.append("URL must point to privacy-policy.html or account-deletion.html")

    return failures


def decode_body(raw: bytes, content_type: str) -> str:
    """Decode response bytes as text, defaulting to UTF-8."""

    charset = "utf-8"
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip() or "utf-8"
            break
    return raw.decode(charset, errors="replace")


def validate_page_content(
    *,
    url: str,
    content_type: str,
    body: str,
    allow_placeholders: bool,
) -> list[str]:
    """Validate fetched HTML body content for Play submission readiness."""

    failures: list[str] = []
    normalized_body = unescape(body)
    page_key = expected_page_key(url)

    if "html" not in content_type.lower():
        failures.append(f"response must be HTML, got Content-Type: {content_type or 'missing'}")

    if page_key is None:
        failures.append("could not determine required phrase set for URL")
    else:
        for phrase in REQUIRED_PHRASES[page_key]:
            if phrase not in normalized_body:
                failures.append(f"missing required phrase: {phrase}")

    if not allow_placeholders:
        for label, pattern in BLOCKED_PATTERNS.items():
            if pattern.search(normalized_body):
                failures.append(f"unresolved {label}")

    return failures


def fetch_url(url: str, *, timeout_seconds: float) -> tuple[int, str, bytes]:
    """Fetch a URL and return status, content type, and limited body bytes."""

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        status_code = int(getattr(response, "status", response.getcode()))
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        return status_code, content_type, raw


def check_page(
    *,
    label: str,
    url: str,
    timeout_seconds: float,
    allow_placeholders: bool,
) -> PageCheckResult:
    """Check one public page URL."""

    failures = validate_url_shape(url)
    if failures:
        return PageCheckResult(label, url, None, "", tuple(failures))

    try:
        status_code, content_type, raw = fetch_url(url, timeout_seconds=timeout_seconds)
    except urllib.error.HTTPError as exc:
        return PageCheckResult(label, url, exc.code, "", (f"HTTP error: {exc.code}",))
    except urllib.error.URLError as exc:
        return PageCheckResult(label, url, None, "", (f"URL error: {exc.reason}",))
    except TimeoutError:
        return PageCheckResult(label, url, None, "", ("request timed out",))

    if len(raw) > MAX_RESPONSE_BYTES:
        failures.append(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
        raw = raw[:MAX_RESPONSE_BYTES]

    if status_code != 200:
        failures.append(f"expected HTTP 200, got {status_code}")

    body = decode_body(raw, content_type)
    failures.extend(
        validate_page_content(
            url=url,
            content_type=content_type,
            body=body,
            allow_placeholders=allow_placeholders,
        )
    )

    return PageCheckResult(label, url, status_code, content_type, tuple(failures))


def print_results(results: Iterable[PageCheckResult]) -> bool:
    """Print result summary. Return True only when every check passed."""

    all_ok = True
    for result in results:
        status = "OK" if result.ok else "FAIL"
        status_code = result.status_code if result.status_code is not None else "no response"
        print(f"[{status}] {result.label}: {result.url}")
        print(f"  status: {status_code}")
        if result.content_type:
            print(f"  content-type: {result.content_type}")
        for failure in result.failures:
            print(f"  - {failure}")
        if result.failures:
            all_ok = False
    return all_ok


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify deployed public Play Store policy/account deletion page URLs."
    )
    parser.add_argument("--privacy-url", default=DEFAULT_PRIVACY_URL)
    parser.add_argument("--account-deletion-url", default=DEFAULT_ACCOUNT_DELETION_URL)
    parser.add_argument("--timeout", type=float, default=10.0, help="Request timeout in seconds")
    parser.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="Only check deployment reachability/shape and required phrases; do not fail on TODOs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = [
        check_page(
            label="Privacy policy",
            url=args.privacy_url,
            timeout_seconds=args.timeout,
            allow_placeholders=args.allow_placeholders,
        ),
        check_page(
            label="Account deletion",
            url=args.account_deletion_url,
            timeout_seconds=args.timeout,
            allow_placeholders=args.allow_placeholders,
        ),
    ]

    if print_results(results):
        print("Public page URLs passed verification.")
        return 0

    print("Public page URLs are not Play submission ready.")
    if not args.allow_placeholders:
        print("Use --allow-placeholders only for deployment smoke tests before final values exist.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
