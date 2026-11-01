package com.aixion.controltower.core.api.dto

data class ExternalAgentDto(
    val id: String,
    val provider: String,
    val display_name: String,
    val auth_type: String,
    val allowed_project_ids: List<String> = emptyList(),
    val allowed_repositories: List<String> = emptyList(),
    val allowed_actions: List<String> = emptyList(),
    val enabled: Boolean = true,
    val created_at: String? = null,
    val updated_at: String? = null
)

data class AgentCreateRequestDto(
    val provider: String = "ANTIGRAVITY",
    val display_name: String,
    val auth_type: String = "API_KEY",
    val allowed_project_ids: List<String>,
    val allowed_repositories: List<String> = emptyList(),
    val allowed_actions: List<String> = ANTIGRAVITY_APPROVAL_ACTIONS,
    val enabled: Boolean = true,
    val rate_limit_per_minute: Int = 60
)

data class AgentRegistrationResponseDto(
    val agent: ExternalAgentDto,
    val agent_token: String? = null
)

data class AgentCredentialStatusDto(
    val agent_id: String,
    val auth_type: String,
    val credential_state: String,
    val token_present: Boolean,
    val token_expires_at: String? = null,
    val token_revoked_at: String? = null,
    val token_rotated_at: String? = null,
    val last_used_at: String? = null,
    val rate_limit_per_minute: Int = 60
)

val ANTIGRAVITY_APPROVAL_ACTIONS = listOf(
    "CREATE_APPROVAL",
    "READ_APPROVAL_DECISION",
    "CONSUME_APPROVAL_DECISION",
    "REPORT_APPROVAL_DELIVERY_FAILURE",
    "EXPIRE_OWN_APPROVAL"
)

val LOCAL_APPROVAL_CONNECTOR_PROVIDERS = listOf("ANTIGRAVITY", "CODEX")

fun isExpectedApprovalConnectorAgent(
    agent: ExternalAgentDto,
    projectId: String,
    provider: String
): Boolean =
    agent.id.isNotBlank() &&
        provider in LOCAL_APPROVAL_CONNECTOR_PROVIDERS &&
        agent.provider == provider &&
        agent.auth_type == "API_KEY" &&
        agent.enabled &&
        agent.allowed_project_ids == listOf(projectId) &&
        agent.allowed_actions.size == ANTIGRAVITY_APPROVAL_ACTIONS.size &&
        agent.allowed_actions.toSet() == ANTIGRAVITY_APPROVAL_ACTIONS.toSet()

fun isExpectedAntigravityApprovalAgent(agent: ExternalAgentDto, projectId: String): Boolean =
    isExpectedApprovalConnectorAgent(agent, projectId, "ANTIGRAVITY")
