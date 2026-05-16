package com.aixion.controltower.core.api.dto

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
