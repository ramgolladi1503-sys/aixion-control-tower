from __future__ import annotations

import pytest

from scripts.print_phase0_real_device_smoke import build_plan, normalize_lan_ip


def test_normalize_lan_ip_accepts_plain_private_lan_ip() -> None:
    assert normalize_lan_ip("192.168.1.20") == "192.168.1.20"


def test_normalize_lan_ip_accepts_url_shape_and_strips_port() -> None:
    assert normalize_lan_ip("http://192.168.1.20:8000/") == "192.168.1.20"


def test_normalize_lan_ip_rejects_localhost() -> None:
    with pytest.raises(ValueError, match="laptop LAN IP"):
        normalize_lan_ip("127.0.0.1")


def test_normalize_lan_ip_rejects_android_emulator_alias_for_physical_phone() -> None:
    with pytest.raises(ValueError, match="Android emulator"):
        normalize_lan_ip("10.0.2.2")


def test_build_plan_renders_copy_paste_commands() -> None:
    plan = build_plan("192.168.1.20")
    output = plan.render_markdown()

    assert "http://192.168.1.20:8000/health" in output
    assert "AIXION_AUTH_ENABLED=false" in output
    assert "uvicorn app.main:app --host 0.0.0.0 --port 8000" in output
    assert "./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.20:8000/" in output
    assert "python scripts/validate_live_external_agent.py" in output
    assert "--mode phase0-lan" in output
    assert "--skip-webhook" in output
    assert "Home loads backend data" in output
    assert "Aixion is connected to ChatGPT/Codex over the public internet" in output


def test_build_plan_rejects_invalid_port() -> None:
    with pytest.raises(ValueError, match="Port"):
        build_plan("192.168.1.20", port=0)
