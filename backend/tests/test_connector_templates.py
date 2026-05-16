from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.connector_templates import connector_templates
from app.main import app
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def test_connector_templates_are_listed_before_dynamic_connector_routes() -> None:
    response = client.get("/connectors/templates")

    assert response.status_code == 200
    body = response.json()
    template_ids = {template["id"] for template in body["templates"]}
    assert "chatgpt-actions-bridge" in template_ids
    assert "codex-agent-bridge" in template_ids
    assert "openclaw-local-bridge" in template_ids
    assert "antigravity-workspace-bridge" in template_ids
    assert "gemini-custom-agent" in template_ids
    assert "claude-cursor-agent" in template_ids
    assert "local-agent-bridge" in template_ids


def test_get_openclaw_template_contains_connector_defaults_mapper_and_sample_payload() -> None:
    response = client.get("/connectors/templates/openclaw-local-bridge")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "openclaw-local-bridge"
    assert body["provider_label"] == "OPENCLAW"
    assert body["connector_type"] == "LOCAL_BRIDGE"
    assert body["auth_type"] == "HMAC"
    assert body["connector_defaults"]["name"] == "OpenClaw Local Bridge"
    assert body["connector_defaults"]["allowed_actions"] == ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"]
    assert body["mapper"]["default_action"] == "CREATE_AGENT_TASK"
    assert body["mapper"]["field_paths"]["title"] == "task.title"
    assert body["mapper"]["defaults"]["requires_approval"] is True
    assert body["sample_payload"]["metadata"]["source"] == "openclaw"
    assert "approval-gated" in str(body["connector_defaults"])


def test_chatgpt_template_is_explicit_and_approval_gated() -> None:
    response = client.get("/connectors/templates/chatgpt-actions-bridge")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "chatgpt-actions-bridge"
    assert body["display_name"] == "ChatGPT Actions Bridge"
    assert body["provider_label"] == "CHATGPT"
    assert body["connector_type"] == "GPT_ACTIONS"
    assert body["auth_type"] == "BEARER"
    assert body["connector_defaults"]["config"]["profile"] == "chatgpt-actions"
    assert body["mapper"]["field_paths"]["title"] == "task.title"
    assert body["mapper"]["field_paths"]["repository"] == "target.repository"
    assert body["mapper"]["defaults"]["requires_approval"] is True
    assert body["sample_payload"]["metadata"]["source"] == "chatgpt"


def test_codex_template_is_explicit_and_approval_gated() -> None:
    response = client.get("/connectors/templates/codex-agent-bridge")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "codex-agent-bridge"
    assert body["display_name"] == "Codex Agent Bridge"
    assert body["provider_label"] == "CODEX"
    assert body["connector_type"] == "WEBHOOK"
    assert body["auth_type"] == "BEARER"
    assert body["connector_defaults"]["config"]["profile"] == "codex-agent"
    assert body["mapper"]["field_paths"]["title"] == "job.title"
    assert body["mapper"]["field_paths"]["repository"] == "git.repository"
    assert body["mapper"]["defaults"]["requires_approval"] is True
    assert body["sample_payload"]["job"]["metadata"]["source"] == "codex"


def test_connector_template_catalog_has_unique_ids_and_valid_mappers() -> None:
    templates = connector_templates()
    ids = [template.id for template in templates]

    assert len(ids) == len(set(ids))
    for template in templates:
        assert template.display_name
        assert template.connector_defaults["provider_label"] == template.provider_label.value
        assert template.connector_defaults["connector_type"] == template.connector_type.value
        assert template.mapper.enabled is True
        assert template.mapper.default_action == "CREATE_AGENT_TASK"
        assert template.sample_payload
        assert template.setup_notes


def test_unknown_connector_template_returns_404() -> None:
    response = client.get("/connectors/templates/not-real")

    assert response.status_code == 404
    assert response.json()["detail"] == "Connector template not found"
