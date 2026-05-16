package com.aixion.controltower.core.api.dto

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
