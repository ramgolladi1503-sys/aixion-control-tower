package com.aixion.controltower.core.api.dto

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
