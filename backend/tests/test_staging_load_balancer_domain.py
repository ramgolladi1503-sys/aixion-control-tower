from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOAD_BALANCER_PATH = ROOT / "deploy/gcp/cloud-run-load-balancer-staging.tf"


def _api_domain_variable_body() -> str:
    text = LOAD_BALANCER_PATH.read_text(encoding="utf-8")
    match = re.search(r'variable "api_domain"\s*\{(?P<body>.*?)\n\}', text, re.DOTALL)
    assert match is not None
    return match.group("body")


def test_staging_load_balancer_requires_explicit_hostname() -> None:
    body = _api_domain_variable_body()

    assert re.search(r"(?m)^\s*default\s*=", body) is None


def test_staging_load_balancer_requires_owned_staging_hostname() -> None:
    body = _api_domain_variable_body()

    assert 'endswith(var.api_domain, ".aixionlab.com")' in body
    assert 'can(regex("(^|[.-])(staging|stage)([.-]|$)", var.api_domain))' in body
    assert 'length(split(".", var.api_domain)) >= 3' in body


def test_staging_load_balancer_requires_staging_label_under_owned_domain() -> None:
    body = _api_domain_variable_body()

    assert 'endswith(var.api_domain, ".aixionlab.com")' in body
    assert 'can(regex("(^|[.-])(staging|stage)([.-]|$)", var.api_domain))' in body
    assert "alltrue([" in body
