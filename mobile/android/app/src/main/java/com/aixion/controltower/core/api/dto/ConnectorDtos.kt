package com.aixion.controltower.core.api.dto

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

data class ConnectorSimulationRequestDto(
    val sample_payload: Map<String, Any?> = emptyMap(),
    val mapper: ConnectorSchemaMapperDto? = null,
    val validate_secret_configured: Boolean = true
)

data class ConnectorSimulationResponseDto(
    val accepted: Boolean = false,
    val connector_id: String,
    val action: String? = null,
    val normalized_payload: Map<String, Any?> = emptyMap(),
    val task_preview: Map<String, Any?>? = null,
    val event_preview: Map<String, Any?>? = null,
    val auth_ready: Boolean = false,
    val scope_ready: Boolean = false,
    val errors: List<String> = emptyList(),
    val warnings: List<String> = emptyList()
)
