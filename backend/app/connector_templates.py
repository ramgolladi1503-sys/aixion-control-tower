from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .connector_models import ConnectorAuthType, ConnectorProviderLabel, ConnectorType
from .connector_schema_mapper import ConnectorSchemaMapperConfig
from .models import AgentAction


class ConnectorTemplate(BaseModel):
    id: str
    display_name: str
    description: str
    provider_label: ConnectorProviderLabel
    connector_type: ConnectorType
    auth_type: ConnectorAuthType
    connector_defaults: dict[str, Any] = Field(default_factory=dict)
    mapper: ConnectorSchemaMapperConfig
    sample_payload: dict[str, Any] = Field(default_factory=dict)
    setup_notes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ConnectorTemplateList(BaseModel):
    templates: list[ConnectorTemplate]


def _template(
    *,
    template_id: str,
    display_name: str,
    description: str,
    provider_label: ConnectorProviderLabel,
    connector_type: ConnectorType,
    auth_type: ConnectorAuthType,
    connector_defaults: dict[str, Any],
    mapper: ConnectorSchemaMapperConfig,
    sample_payload: dict[str, Any],
    setup_notes: list[str],
    tags: list[str],
) -> ConnectorTemplate:
    return ConnectorTemplate(
        id=template_id,
        display_name=display_name,
        description=description,
        provider_label=provider_label,
        connector_type=connector_type,
        auth_type=auth_type,
        connector_defaults=connector_defaults,
        mapper=mapper,
        sample_payload=sample_payload,
        setup_notes=setup_notes,
        tags=tags,
    )


def _base_defaults(
    *,
    name: str,
    connector_type: ConnectorType,
    provider_label: ConnectorProviderLabel,
    auth_type: ConnectorAuthType,
    profile: str,
    actions: list[AgentAction] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "connector_type": connector_type.value,
        "provider_label": provider_label.value,
        "auth_type": auth_type.value,
        "allowed_actions": [action.value for action in (actions or [AgentAction.CREATE_AGENT_TASK, AgentAction.APPEND_AGENT_TASK_EVENT, AgentAction.READ_AGENT_TASK])],
        "rate_limit_per_minute": 60,
        "enabled": True,
        "config": {"profile": profile, "mode": "approval-gated"},
    }


def connector_templates() -> list[ConnectorTemplate]:
    return [
        _template(
            template_id="openclaw-local-bridge",
            display_name="OpenClaw Local Bridge",
            description="Local OpenClaw-style agent bridge that creates approval-gated AgentTasks.",
            provider_label=ConnectorProviderLabel.OPENCLAW,
            connector_type=ConnectorType.LOCAL_BRIDGE,
            auth_type=ConnectorAuthType.HMAC,
            connector_defaults=_base_defaults(
                name="OpenClaw Local Bridge",
                connector_type=ConnectorType.LOCAL_BRIDGE,
                provider_label=ConnectorProviderLabel.OPENCLAW,
                auth_type=ConnectorAuthType.HMAC,
                profile="openclaw",
            ),
            mapper=ConnectorSchemaMapperConfig(
                default_action=AgentAction.CREATE_AGENT_TASK,
                field_paths={
                    "title": "task.title",
                    "goal": "task.goal",
                    "context": "task.context",
                    "repository": "repo.full_name",
                    "branch_preference": "repo.branch",
                    "metadata": "metadata",
                },
                defaults={"requested_action": "GENERATE_DOCS", "requires_approval": True},
            ),
            sample_payload={
                "task": {"title": "Review generated code", "goal": "Prepare a safe implementation plan", "context": "OpenClaw local run"},
                "repo": {"full_name": "owner/repo", "branch": "feature/openclaw-task"},
                "metadata": {"source": "openclaw"},
            },
            setup_notes=[
                "Use HMAC auth for local bridge calls.",
                "Keep mutating actions approval-gated.",
                "Expose the webhook only through a trusted tunnel or private network until public callback hardening is complete.",
            ],
            tags=["openclaw", "local", "bridge"],
        ),
        _template(
            template_id="antigravity-workspace-bridge",
            display_name="Antigravity Workspace Bridge",
            description="Agent-workspace connector for IDE-style planning and artifact handoff.",
            provider_label=ConnectorProviderLabel.ANTIGRAVITY,
            connector_type=ConnectorType.WEBHOOK,
            auth_type=ConnectorAuthType.BEARER,
            connector_defaults=_base_defaults(
                name="Antigravity Workspace Bridge",
                connector_type=ConnectorType.WEBHOOK,
                provider_label=ConnectorProviderLabel.ANTIGRAVITY,
                auth_type=ConnectorAuthType.BEARER,
                profile="antigravity",
            ),
            mapper=ConnectorSchemaMapperConfig(
                default_action=AgentAction.CREATE_AGENT_TASK,
                field_paths={
                    "title": "workspace.task.title",
                    "goal": "workspace.task.objective",
                    "context": "workspace.summary",
                    "repository": "workspace.repository",
                    "branch_preference": "workspace.branch",
                    "metadata": "artifacts",
                },
                defaults={"requested_action": "GENERATE_DOCS", "requires_approval": True},
            ),
            sample_payload={
                "workspace": {
                    "task": {"title": "Refactor approval screen", "objective": "Prepare implementation plan and evidence"},
                    "summary": "IDE agent produced planning artifacts.",
                    "repository": "owner/repo",
                    "branch": "feature/approval-refactor",
                },
                "artifacts": {"plan_url": "https://example.com/artifact"},
            },
            setup_notes=[
                "Use bearer auth for workspace bridge callbacks.",
                "Map artifacts into metadata until artifact storage is implemented.",
                "Use schema preview before enabling live webhook traffic.",
            ],
            tags=["antigravity", "ide", "workspace"],
        ),
        _template(
            template_id="gemini-custom-agent",
            display_name="Gemini Custom Agent",
            description="Generic Gemini-based agent webhook template for task creation.",
            provider_label=ConnectorProviderLabel.GEMINI,
            connector_type=ConnectorType.GENERIC_HTTP,
            auth_type=ConnectorAuthType.BEARER,
            connector_defaults=_base_defaults(
                name="Gemini Custom Agent",
                connector_type=ConnectorType.GENERIC_HTTP,
                provider_label=ConnectorProviderLabel.GEMINI,
                auth_type=ConnectorAuthType.BEARER,
                profile="gemini-custom",
            ),
            mapper=ConnectorSchemaMapperConfig(
                default_action=AgentAction.CREATE_AGENT_TASK,
                field_paths={
                    "title": "request.title",
                    "goal": "request.goal",
                    "context": "request.context",
                    "repository": "target.repository",
                    "metadata": "request.metadata",
                },
                defaults={"requested_action": "GENERATE_DOCS", "requires_approval": True},
            ),
            sample_payload={
                "request": {"title": "Generate test plan", "goal": "Prepare QA evidence", "context": "Gemini agent request", "metadata": {"model": "gemini"}},
                "target": {"repository": "owner/repo"},
            },
            setup_notes=[
                "Use this for Gemini-backed custom tools that can send HTTP callbacks.",
                "Keep project and repository scopes narrow.",
            ],
            tags=["gemini", "custom", "http"],
        ),
        _template(
            template_id="claude-cursor-agent",
            display_name="Claude or Cursor Agent",
            description="Claude/Cursor-style coding assistant connector template.",
            provider_label=ConnectorProviderLabel.CLAUDE,
            connector_type=ConnectorType.WEBHOOK,
            auth_type=ConnectorAuthType.BEARER,
            connector_defaults=_base_defaults(
                name="Claude or Cursor Agent",
                connector_type=ConnectorType.WEBHOOK,
                provider_label=ConnectorProviderLabel.CLAUDE,
                auth_type=ConnectorAuthType.BEARER,
                profile="claude-cursor",
            ),
            mapper=ConnectorSchemaMapperConfig(
                default_action=AgentAction.CREATE_AGENT_TASK,
                field_paths={
                    "title": "job.name",
                    "goal": "job.instructions",
                    "context": "job.context",
                    "repository": "git.repo",
                    "branch_preference": "git.branch",
                    "metadata": "job.artifacts",
                },
                defaults={"requested_action": "GENERATE_DOCS", "requires_approval": True},
            ),
            sample_payload={
                "job": {"name": "Patch failing test", "instructions": "Find cause and propose fix", "context": "Coding assistant", "artifacts": {"trace": "summary"}},
                "git": {"repo": "owner/repo", "branch": "feature/fix-test"},
            },
            setup_notes=[
                "Use this as a base for Claude, Cursor, or similar coding-agent callbacks.",
                "Change provider label later if a dedicated Cursor profile is needed.",
            ],
            tags=["claude", "cursor", "coding-agent"],
        ),
        _template(
            template_id="local-agent-bridge",
            display_name="Local Agent Bridge",
            description="Generic local process bridge for homegrown agents and scripts.",
            provider_label=ConnectorProviderLabel.CUSTOM,
            connector_type=ConnectorType.LOCAL_BRIDGE,
            auth_type=ConnectorAuthType.HMAC,
            connector_defaults=_base_defaults(
                name="Local Agent Bridge",
                connector_type=ConnectorType.LOCAL_BRIDGE,
                provider_label=ConnectorProviderLabel.CUSTOM,
                auth_type=ConnectorAuthType.HMAC,
                profile="local-bridge",
            ),
            mapper=ConnectorSchemaMapperConfig(
                default_action=AgentAction.CREATE_AGENT_TASK,
                field_paths={
                    "title": "title",
                    "goal": "goal",
                    "context": "context",
                    "repository": "repository",
                    "metadata": "metadata",
                },
                defaults={"requested_action": "GENERATE_DOCS", "requires_approval": True},
            ),
            sample_payload={
                "title": "Local agent task",
                "goal": "Submit task from local script",
                "context": "Local bridge payload",
                "repository": "owner/repo",
                "metadata": {"source": "local"},
            },
            setup_notes=[
                "Best for scripts running on your own machine.",
                "Use HMAC and keep callback surface private.",
            ],
            tags=["local", "custom", "bridge"],
        ),
    ]


def get_connector_template(template_id: str) -> ConnectorTemplate:
    for template in connector_templates():
        if template.id == template_id:
            return template
    raise HTTPException(status_code=404, detail="Connector template not found")
