from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_public_page_urls.py"
spec = importlib.util.spec_from_file_location("verify_public_page_urls", SCRIPT_PATH)
assert spec is not None
verifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)


def test_validate_url_shape_requires_https() -> None:
    failures = verifier.validate_url_shape(
        "http://ramgolladi1503-sys.github.io/aixion-control-tower/privacy-policy.html"
    )

    assert "URL must use HTTPS" in failures


def test_validate_url_shape_rejects_pdf() -> None:
    failures = verifier.validate_url_shape("https://example.test/privacy-policy.pdf")

    assert "Google Play policy/account deletion URL must not be a PDF" in failures


def test_validate_page_content_blocks_placeholders_by_default() -> None:
    body = """
    <html>
      <body>
        <h1>Privacy Policy</h1>
        <p>Aixion Control Tower</p>
        <p>Account deletion</p>
        <p>We do not sell user data</p>
        <p>retention</p>
        <p>TODO: support/privacy email</p>
      </body>
    </html>
    """

    failures = verifier.validate_page_content(
        url="https://example.test/privacy-policy.html",
        content_type="text/html; charset=utf-8",
        body=body,
        allow_placeholders=False,
    )

    assert "unresolved TODO placeholder" in failures
    assert "unresolved placeholder support email" in failures


def test_validate_page_content_allows_placeholders_for_smoke_mode() -> None:
    body = """
    <html>
      <body>
        <h1>Privacy Policy</h1>
        <p>Aixion Control Tower</p>
        <p>Account deletion</p>
        <p>We do not sell user data</p>
        <p>retention</p>
        <p>TODO: support/privacy email</p>
      </body>
    </html>
    """

    failures = verifier.validate_page_content(
        url="https://example.test/privacy-policy.html",
        content_type="text/html; charset=utf-8",
        body=body,
        allow_placeholders=True,
    )

    assert failures == []


def test_validate_page_content_requires_html() -> None:
    failures = verifier.validate_page_content(
        url="https://example.test/account-deletion.html",
        content_type="text/plain",
        body=(
            "Aixion Control Tower Account Deletion Request Inside the app "
            "Outside the app auth.account_deletion_requested"
        ),
        allow_placeholders=True,
    )

    assert "response must be HTML, got Content-Type: text/plain" in failures


def test_check_page_returns_shape_failures_without_network() -> None:
    result = verifier.check_page(
        label="Privacy policy",
        url="https://example.test/not-the-right-page.html",
        timeout_seconds=0.01,
        allow_placeholders=True,
    )

    assert not result.ok
    assert result.status_code is None
    assert "URL must point to privacy-policy.html or account-deletion.html" in result.failures
