package com.aixion.controltower.core.api.dto

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
