package com.aixion.controltower.core.api.dto

data class ProjectDto(
    val id: String,
    val name: String,
    val description: String? = null,
    val mode: String? = null
)

data class ProjectCreateDto(
    val name: String,
    val description: String = "",
    val mode: String = "STRICT",
    val rules: List<String> = emptyList()
)

data class IdeaDto(
    val id: String,
    val project_id: String? = null,
    val title: String,
    val raw_text: String
)

data class IdeaCreateDto(
    val project_id: String? = null,
    val title: String,
    val raw_text: String
)

data class WorkOrderDto(
    val id: String,
    val project_id: String,
    val idea_id: String? = null,
    val goal: String,
    val context: String? = null,
    val tasks: List<String> = emptyList(),
    val affected_areas: List<String> = emptyList(),
    val required_tests: List<String> = emptyList(),
    val rollback_plan: String? = null,
    val risk_level: String? = null
)

data class WorkOrderCreateDto(
    val project_id: String,
    val idea_id: String? = null,
    val goal: String,
    val context: String = "",
    val tasks: List<String> = emptyList(),
    val affected_areas: List<String> = emptyList(),
    val required_tests: List<String> = emptyList(),
    val rollback_plan: String = ""
)

data class FileChangeDto(
    val path: String,
    val change_type: String = "update",
    val diff: String
)

data class RiskAssessmentDto(
    val level: String,
    val reasons: List<String> = emptyList(),
    val blocked: Boolean = false,
    val required_actions: List<String> = emptyList()
)

data class ApprovalRequestDto(
    val id: String,
    val project_id: String,
    val work_order_id: String? = null,
    val title: String,
    val summary: String,
    val agent_name: String = "agent",
    val target_branch: String,
    val files: List<FileChangeDto> = emptyList(),
    val test_plan: List<String> = emptyList(),
    val rollback_plan: String? = null,
    val status: String,
    val risk: RiskAssessmentDto,
    val source_provider: String? = null,
    val source_agent_id: String? = null,
    val source_agent_name: String? = null,
    val source_session_id: String? = null,
    val source_task_url: String? = null,
    val created_by_user_id: String? = null,
    val verified_source: Boolean = false
) {
    val uiSourceProvider: String get() = source_provider ?: "MANUAL"
    val uiSourceAgentId: String? get() = source_agent_id
    val uiSourceAgentName: String? get() = source_agent_name
    val uiSourceSessionId: String? get() = source_session_id
    val uiSourceTaskUrl: String? get() = source_task_url
    val uiCreatedByUserId: String? get() = created_by_user_id
    val uiVerifiedSource: Boolean get() = verified_source
}

data class DecisionRequestDto(
    val decision: String,
    val reason: String = ""
)

data class AuthUserDto(
    val id: String,
    val email: String,
    val display_name: String,
    val role: String
)

data class LoginRequestDto(
    val email: String,
    val password: String
)

data class RegisterRequestDto(
    val email: String,
    val password: String,
    val display_name: String = "",
    val invite_token: String? = null
)

data class AuthResponseDto(
    val access_token: String,
    val token_type: String = "bearer",
    val user: AuthUserDto
)

data class RoleChoicesDto(
    val roles: List<String> = emptyList()
)

data class RoleUpdateRequestDto(
    val role: String
)

data class InviteCreateRequestDto(
    val email: String,
    val role: String = "REVIEWER",
    val expires_in_days: Int = 7
)

data class InviteDto(
    val id: String,
    val email: String,
    val role: String,
    val status: String,
    val expires_at: String? = null,
    val created_by_user_id: String,
    val accepted_by_user_id: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null
)

data class InviteCreateResponseDto(
    val id: String,
    val email: String,
    val role: String,
    val status: String,
    val expires_at: String? = null,
    val created_by_user_id: String,
    val accepted_by_user_id: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null,
    val token: String
)

data class SessionDto(
    val id: String,
    val user_id: String,
    val user_email: String,
    val user_role: String,
    val created_at: String? = null,
    val expires_at: String? = null,
    val revoked: Boolean = false,
    val active: Boolean = false
)

data class SessionRevokeResponseDto(
    val target_user_id: String,
    val target_email: String,
    val revoked_sessions_count: Int
)

data class TestRunDto(
    val id: String,
    val approval_request_id: String,
    val command: String,
    val status: String,
    val output_summary: String? = null
)

data class AuditEventDto(
    val id: String,
    val event_type: String,
    val actor: String = "system",
    val entity_id: String,
    val details: Map<String, Any?> = emptyMap(),
    val created_at: String? = null
)


data class RuntimeReadinessDto(
    val status: String,
    val generated_at: String? = null,
    val profile: String,
    val auth_enabled: Boolean,
    val db_reachable: Boolean,
    val migrations_applied: Boolean,
    val expected_migration_ids: List<String> = emptyList(),
    val applied_migration_ids: List<String> = emptyList(),
    val recovery_snapshot_available: Boolean,
    val recovery_format_version: String,
    val github_token_configured: Boolean,
    val fcm_server_key_configured: Boolean,
    val errors: List<String> = emptyList(),
    val warnings: List<String> = emptyList()
) {
    val isReady: Boolean
        get() = status.equals("ready", ignoreCase = true) && errors.isEmpty()
}

data class AgentTaskDto(
    val id: String,
    val provider: String = "MANUAL",
    val project_id: String? = null,
    val title: String,
    val goal: String,
    val context: String = "",
    val source_url: String? = null,
    val source_session_id: String? = null,
    val source_task_id: String? = null,
    val requested_action: String = "CREATE_WORK_ORDER",
    val repository: String? = null,
    val branch_preference: String? = null,
    val risk_hint: String? = null,
    val requires_approval: Boolean = true,
    val metadata: Map<String, Any?> = emptyMap(),
    val status: String = "RECEIVED",
    val external_agent_id: String? = null,
    val external_agent_name: String? = null,
    val approval_request_id: String? = null,
    val created_by_user_id: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null
)

data class AgentTaskEventDto(
    val id: String,
    val task_id: String,
    val event_type: String = "NOTE",
    val message: String = "",
    val status: String? = null,
    val metadata: Map<String, Any?> = emptyMap(),
    val actor: String = "system",
    val created_at: String? = null
)

data class MCPPendingRequestDto(
    val id: String,
    val project_id: String,
    val approval_request_id: String,
    val server_name: String,
    val tool_name: String,
    val arguments: Map<String, Any?> = emptyMap(),
    val session_id: String? = null,
    val requested_by: String = "mcp-client",
    val status: String,
    val attempts: Int = 0,
    val max_attempts: Int = 3,
    val lease_owner: String? = null,
    val lease_expires_at: String? = null,
    val last_error: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null
)

data class MCPPendingHealthDto(
    val total: Int = 0,
    val by_status: Map<String, Int> = emptyMap(),
    val waiting_for_approval: Int = 0,
    val forwarding: Int = 0,
    val active_leases: Int = 0,
    val expired_leases: Int = 0,
    val retryable: Int = 0,
    val dead_letter: Int = 0,
    val terminal: Int = 0,
    val attention_required: Int = 0,
    val oldest_waiting_created_at: String? = null,
    val oldest_dead_letter_created_at: String? = null
)

data class PendingRetryRequestDto(
    val reason: String = "",
    val reset_attempts: Boolean = true
)

data class MCPGatewayDecisionDto(
    val forwarded: Boolean = false,
    val approval_required: Boolean = true,
    val approval_request_id: String? = null,
    val status: String? = null,
    val result: Map<String, Any?>? = null,
    val reason: String = ""
)

data class ConnectorDto(
    val id: String,
    val name: String,
    val connector_type: String,
    val provider_label: String,
    val endpoint_url: String? = null,
    val callback_url: String? = null,
    val auth_type: String,
    val status: String,
    val health_status: String,
    val allowed_project_ids: List<String> = emptyList(),
    val allowed_repositories: List<String> = emptyList(),
    val allowed_actions: List<String> = emptyList(),
    val rate_limit_per_minute: Int = 60,
    val secret_configured: Boolean = false,
    val last_used_at: String? = null,
    val last_health_check_at: String? = null,
    val failed_auth_count: Int = 0,
    val last_error: String? = null,
    val config: Map<String, Any?> = emptyMap(),
    val created_at: String? = null,
    val updated_at: String? = null
)

data class ConnectorCreateDto(
    val name: String,
    val connector_type: String = "GENERIC_HTTP",
    val provider_label: String = "CUSTOM",
    val endpoint_url: String? = null,
    val callback_url: String? = null,
    val auth_type: String = "BEARER",
    val allowed_project_ids: List<String> = emptyList(),
    val allowed_repositories: List<String> = emptyList(),
    val allowed_actions: List<String> = listOf("CREATE_AGENT_TASK"),
    val rate_limit_per_minute: Int = 60,
    val enabled: Boolean = true,
    val config: Map<String, Any?> = emptyMap()
)

data class ConnectorSecretRequestDto(val note: String = "")

data class ConnectorSecretIssueResponseDto(
    val connector: ConnectorDto,
    val secret: String? = null,
    val secret_hint: String? = null
)

data class ConnectorCredentialStatusDto(
    val connector_id: String,
    val auth_type: String,
    val secret_configured: Boolean,
    val secret_revoked_at: String? = null,
    val secret_rotated_at: String? = null,
    val last_used_at: String? = null,
    val last_health_check_at: String? = null,
    val failed_auth_count: Int = 0,
    val last_error: String? = null,
    val health_status: String,
    val status: String
)

data class ConnectorTemplateListDto(val templates: List<ConnectorTemplateDto> = emptyList())

data class ConnectorTemplateDto(
    val id: String,
    val display_name: String,
    val description: String,
    val provider_label: String,
    val connector_type: String,
    val auth_type: String,
    val connector_defaults: ConnectorCreateDto,
    val mapper: ConnectorSchemaMapperDto,
    val sample_payload: Map<String, Any?> = emptyMap(),
    val setup_notes: List<String> = emptyList(),
    val tags: List<String> = emptyList()
)

data class ConnectorSchemaMapperDto(
    val enabled: Boolean = true,
    val default_action: String = "CREATE_AGENT_TASK",
    val field_paths: Map<String, String> = emptyMap(),
    val defaults: Map<String, Any?> = emptyMap()
)

data class ConnectorSchemaMapperStatusDto(
    val connector_id: String,
    val mapper: ConnectorSchemaMapperDto? = null,
    val mapper_enabled: Boolean = false
)

data class ConnectorSchemaMapperPreviewRequestDto(
    val sample_payload: Map<String, Any?> = emptyMap(),
    val mapper: ConnectorSchemaMapperDto? = null
)

data class ConnectorSchemaMapperPreviewResponseDto(
    val normalized_payload: Map<String, Any?> = emptyMap(),
    val mapper_enabled: Boolean = false,
    val warnings: List<String> = emptyList()
)
