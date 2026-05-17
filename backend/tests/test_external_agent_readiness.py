from __future__ import annotations

from app.external_agent_readiness import build_external_agent_readiness
from app.settings import Settings


def _settings(
    *,
    public_base_url: str | None,
    auth_enabled: bool = True,
    demo_override: bool = False,
    validation_errors: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        profile="test",
        auth_enabled=auth_enabled,
        db_path="runtime/aixion_control_tower_test.sqlite3",
        public_base_url=public_base_url,
        allow_unauthenticated_external_agent_demo=demo_override,
        validation_errors=validation_errors,
    )


def test_external_agent_readiness_fails_without_public_base_url() -> None:
    readiness = build_external_agent_readiness(_settings(public_base_url=None))

    assert readiness.status == "not_ready"
    assert readiness.public_base_url_configured is False
    assert "AIXION_PUBLIC_BASE_URL is required for external-agent validation." in readiness.errors


def test_external_agent_readiness_rejects_localhost_http_url() -> None:
    readiness = build_external_agent_readiness(_settings(public_base_url="http://localhost:8000"))

    assert readiness.status == "not_ready"
    assert readiness.public_base_url_https is False
    assert readiness.public_base_url_public_host is False
    assert "AIXION_PUBLIC_BASE_URL must use https for ChatGPT/Codex external callbacks." in readiness.errors
    assert "AIXION_PUBLIC_BASE_URL must use a public hostname, not localhost, LAN IP, or private IP." in readiness.errors


def test_external_agent_readiness_rejects_private_ip_even_with_https() -> None:
    readiness = build_external_agent_readiness(_settings(public_base_url="https://192.168.1.20"))

    assert readiness.status == "not_ready"
    assert readiness.public_base_url_https is True
    assert readiness.public_base_url_public_host is False
    assert "AIXION_PUBLIC_BASE_URL must use a public hostname, not localhost, LAN IP, or private IP." in readiness.errors


def test_external_agent_readiness_requires_auth_unless_demo_override() -> None:
    readiness = build_external_agent_readiness(_settings(public_base_url="https://api.example.com", auth_enabled=False))

    assert readiness.status == "not_ready"
    assert "AIXION_AUTH_ENABLED must be true for external-agent readiness unless the explicit demo override is enabled." in readiness.errors


def test_external_agent_readiness_allows_explicit_unauthenticated_demo_override_with_warning() -> None:
    readiness = build_external_agent_readiness(
        _settings(
            public_base_url="https://api.example.com",
            auth_enabled=False,
            demo_override=True,
        )
    )

    assert readiness.status == "ready"
    assert readiness.unauthenticated_demo_override is True
    assert any("explicit unauthenticated external-agent demo override" in warning for warning in readiness.warnings)


def test_external_agent_readiness_passes_for_public_https_auth_and_required_templates() -> None:
    readiness = build_external_agent_readiness(_settings(public_base_url="https://api.example.com", auth_enabled=True))

    assert readiness.status == "ready"
    assert readiness.public_base_url_configured is True
    assert readiness.public_base_url_https is True
    assert readiness.public_base_url_public_host is True
    assert readiness.auth_enabled is True
    assert readiness.required_templates_present is True
    assert "chatgpt-actions-bridge" in readiness.required_template_ids
    assert "codex-agent-bridge" in readiness.required_template_ids
    assert not readiness.errors


def test_external_agent_readiness_surfaces_existing_environment_validation_errors() -> None:
    readiness = build_external_agent_readiness(
        _settings(
            public_base_url="https://api.example.com",
            validation_errors=("Invalid production setting",),
        )
    )

    assert readiness.status == "not_ready"
    assert "Invalid production setting" in readiness.errors
