from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .connector_templates import connector_templates
from .models import now_utc
from .settings import Settings, get_settings

REQUIRED_EXTERNAL_AGENT_TEMPLATES = {
    "chatgpt-actions-bridge",
    "codex-agent-bridge",
}


class ExternalAgentReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    generated_at: object = Field(default_factory=now_utc)
    profile: str
    public_base_url: str | None
    public_base_url_configured: bool
    public_base_url_https: bool
    public_base_url_public_host: bool
    auth_enabled: bool
    unauthenticated_demo_override: bool
    required_templates_present: bool
    required_template_ids: list[str] = Field(default_factory=list)
    available_template_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _is_public_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip().lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return "." in normalized
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _url_checks(public_base_url: str | None) -> tuple[bool, bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not public_base_url:
        return False, False, ["AIXION_PUBLIC_BASE_URL is required for external-agent validation."], warnings

    parsed = urlparse(public_base_url)
    https = parsed.scheme == "https"
    public_host = _is_public_hostname(parsed.hostname)

    if not https:
        errors.append("AIXION_PUBLIC_BASE_URL must use https for ChatGPT/Codex external callbacks.")
    if not public_host:
        errors.append("AIXION_PUBLIC_BASE_URL must use a public hostname, not localhost, LAN IP, or private IP.")
    if parsed.query or parsed.fragment:
        warnings.append("AIXION_PUBLIC_BASE_URL should be a clean origin/base path without query or fragment.")
    if public_base_url.rstrip("/") != public_base_url:
        warnings.append("AIXION_PUBLIC_BASE_URL should not include a trailing slash.")

    return https, public_host, errors, warnings


def build_external_agent_readiness(settings: Settings | None = None) -> ExternalAgentReadinessResponse:
    current_settings = settings or get_settings()
    errors = list(current_settings.validation_errors)
    warnings: list[str] = []

    public_base_url_https, public_base_url_public_host, url_errors, url_warnings = _url_checks(current_settings.public_base_url)
    errors.extend(url_errors)
    warnings.extend(url_warnings)

    if not current_settings.auth_enabled:
        if current_settings.allow_unauthenticated_external_agent_demo:
            warnings.append("Auth is disabled using explicit unauthenticated external-agent demo override. Do not use this for production.")
        else:
            errors.append("AIXION_AUTH_ENABLED must be true for external-agent readiness unless the explicit demo override is enabled.")

    available_template_ids = sorted(template.id for template in connector_templates())
    missing_templates = sorted(REQUIRED_EXTERNAL_AGENT_TEMPLATES - set(available_template_ids))
    required_templates_present = not missing_templates
    if missing_templates:
        errors.append("Missing required external-agent connector templates: " + ", ".join(missing_templates))

    ready = not errors and public_base_url_https and public_base_url_public_host and required_templates_present

    return ExternalAgentReadinessResponse(
        status="ready" if ready else "not_ready",
        profile=current_settings.profile,
        public_base_url=current_settings.public_base_url,
        public_base_url_configured=bool(current_settings.public_base_url),
        public_base_url_https=public_base_url_https,
        public_base_url_public_host=public_base_url_public_host,
        auth_enabled=current_settings.auth_enabled,
        unauthenticated_demo_override=current_settings.allow_unauthenticated_external_agent_demo,
        required_templates_present=required_templates_present,
        required_template_ids=sorted(REQUIRED_EXTERNAL_AGENT_TEMPLATES),
        available_template_ids=available_template_ids,
        errors=errors,
        warnings=warnings,
    )
