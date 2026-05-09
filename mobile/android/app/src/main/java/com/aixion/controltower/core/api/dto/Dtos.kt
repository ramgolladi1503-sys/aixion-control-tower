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
