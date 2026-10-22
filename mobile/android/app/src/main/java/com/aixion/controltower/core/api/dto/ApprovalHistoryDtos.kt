package com.aixion.controltower.core.api.dto

import com.google.gson.annotations.SerializedName

data class ApprovalHistoryResponseDto(
    val events: List<ApprovalHistoryEventDto> = emptyList(),
    val limit: Int = 50,
    @SerializedName("has_more") val hasMore: Boolean = false,
    @SerializedName("next_cursor") val nextCursor: String? = null
)

data class ApprovalHistoryEventDto(
    @SerializedName("audit_event_id") val auditEventId: String,
    @SerializedName("approval_id") val approvalId: String,
    val decision: String,
    val reason: String? = null,
    @SerializedName("decided_at") val decidedAt: String? = null,
    @SerializedName("reviewer_label") val reviewerLabel: String? = null,
    @SerializedName("request_snapshot_verified") val requestSnapshotVerified: Boolean = false,
    @SerializedName("request_snapshot") val requestSnapshot: ApprovalHistoryRequestSnapshotDto? = null
)

data class ApprovalHistoryRequestSnapshotDto(
    @SerializedName("project_id") val projectId: String? = null,
    val title: String? = null,
    val summary: String? = null,
    @SerializedName("agent_name") val agentName: String? = null,
    @SerializedName("target_branch") val targetBranch: String? = null,
    val files: List<ApprovalHistoryFileDto>? = null,
    @SerializedName("test_plan") val testPlan: List<String>? = null,
    @SerializedName("rollback_plan") val rollbackPlan: String? = null,
    @SerializedName("source_provider") val sourceProvider: String? = null,
    @SerializedName("source_agent_name") val sourceAgentName: String? = null,
    @SerializedName("source_session_id") val sourceSessionId: String? = null,
    @SerializedName("source_invocation_id") val sourceInvocationId: String? = null,
    @SerializedName("source_task_url") val sourceTaskUrl: String? = null,
    val risk: ApprovalHistoryRiskDto? = null
)

data class ApprovalHistoryFileDto(
    val path: String? = null,
    @SerializedName("change_type") val changeType: String? = null
)

data class ApprovalHistoryRiskDto(
    val level: String? = null,
    val reasons: List<String>? = null,
    val blocked: Boolean? = null,
    @SerializedName("required_actions") val requiredActions: List<String>? = null
)
