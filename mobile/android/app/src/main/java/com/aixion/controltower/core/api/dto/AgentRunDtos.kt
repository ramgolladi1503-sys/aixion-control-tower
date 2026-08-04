package com.aixion.controltower.core.api.dto

import com.google.gson.annotations.SerializedName

data class AgentRunDto(
    val id: String,
    @SerializedName("task_id") val taskId: String,
    @SerializedName("approval_request_id") val approvalRequestId: String? = null,
    @SerializedName("capability_lease_id") val capabilityLeaseId: String? = null,
    @SerializedName("project_id") val projectId: String? = null,
    val repository: String? = null,
    val objective: String = "",
    @SerializedName("correlation_id") val correlationId: String,
    val status: String,
    @SerializedName("current_step_id") val currentStepId: String? = null,
    @SerializedName("current_step_index") val currentStepIndex: Int = 0,
    @SerializedName("pause_requested") val pauseRequested: Boolean = false,
    @SerializedName("cancel_requested") val cancelRequested: Boolean = false,
    @SerializedName("lease_owner") val leaseOwner: String? = null,
    @SerializedName("heartbeat_at") val heartbeatAt: String? = null,
    @SerializedName("started_at") val startedAt: String? = null,
    @SerializedName("completed_at") val completedAt: String? = null,
    @SerializedName("final_evidence_hash") val finalEvidenceHash: String? = null,
    @SerializedName("last_error") val lastError: String? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("updated_at") val updatedAt: String? = null
)

data class AgentRunStepDto(
    val id: String,
    @SerializedName("run_id") val runId: String,
    @SerializedName("task_id") val taskId: String,
    val sequence: Int,
    @SerializedName("step_type") val stepType: String,
    val status: String,
    @SerializedName("attempt_count") val attemptCount: Int = 0,
    @SerializedName("max_attempts") val maxAttempts: Int = 3,
    @SerializedName("next_retry_at") val nextRetryAt: String? = null,
    val decision: String? = null,
    val reason: String = "",
    @SerializedName("last_error") val lastError: String? = null,
    @SerializedName("evidence_hash") val evidenceHash: String? = null,
    @SerializedName("output_reference") val outputReference: String? = null,
    @SerializedName("started_at") val startedAt: String? = null,
    @SerializedName("completed_at") val completedAt: String? = null,
    @SerializedName("duration_ms") val durationMs: Long? = null
)

data class AgentRunEventDto(
    val id: String,
    @SerializedName("run_id") val runId: String,
    @SerializedName("task_id") val taskId: String,
    @SerializedName("step_id") val stepId: String? = null,
    @SerializedName("correlation_id") val correlationId: String,
    @SerializedName("event_type") val eventType: String,
    @SerializedName("previous_status") val previousStatus: String? = null,
    @SerializedName("new_status") val newStatus: String? = null,
    val message: String = "",
    val reason: String = "",
    val actor: String = "system",
    @SerializedName("attempt_number") val attemptNumber: Int? = null,
    @SerializedName("created_at") val createdAt: String? = null
)

data class AgentRunDetailDto(
    val run: AgentRunDto,
    val steps: List<AgentRunStepDto> = emptyList(),
    val events: List<AgentRunEventDto> = emptyList()
)

data class AgentRunSummaryDto(
    val total: Int = 0,
    val active: Int = 0,
    @SerializedName("retry_wait") val retryWait: Int = 0,
    @SerializedName("needs_human") val needsHuman: Int = 0,
    val blocked: Int = 0,
    val failed: Int = 0,
    val succeeded: Int = 0,
    val cancelled: Int = 0,
    @SerializedName("queue_depth") val queueDepth: Int = 0
)

data class TrustExceptionDto(
    val id: String,
    val category: String,
    val severity: String,
    val title: String,
    val summary: String,
    @SerializedName("run_id") val runId: String? = null,
    @SerializedName("task_id") val taskId: String? = null,
    @SerializedName("action_id") val actionId: String? = null,
    @SerializedName("lease_id") val leaseId: String? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("requires_human") val requiresHuman: Boolean = true
)

data class AgentReliabilityScorecardDto(
    val provider: String,
    @SerializedName("agent_id") val agentId: String? = null,
    @SerializedName("evaluated_actions") val evaluatedActions: Int = 0,
    @SerializedName("allowed_actions") val allowedActions: Int = 0,
    @SerializedName("blocked_actions") val blockedActions: Int = 0,
    @SerializedName("approval_escalations") val approvalEscalations: Int = 0,
    @SerializedName("scope_adherence_rate") val scopeAdherenceRate: Double = 0.0,
    @SerializedName("first_attempt_success_rate") val firstAttemptSuccessRate: Double = 0.0,
    @SerializedName("recovery_success_rate") val recoverySuccessRate: Double = 0.0,
    @SerializedName("human_intervention_rate") val humanInterventionRate: Double = 0.0,
    @SerializedName("policy_block_rate") val policyBlockRate: Double = 0.0,
    @SerializedName("evidence_completion_rate") val evidenceCompletionRate: Double = 0.0,
    @SerializedName("average_runtime_seconds") val averageRuntimeSeconds: Double = 0.0,
    @SerializedName("total_cost_usd") val totalCostUsd: Double = 0.0,
    val confidence: Double = 0.0,
    @SerializedName("generated_at") val generatedAt: String? = null
)

data class ActionAuthorizationRequestDto(
    val decision: String,
    val reason: String
)

data class ActionAuthorizationDto(
    val id: String,
    @SerializedName("action_id") val actionId: String,
    @SerializedName("policy_decision_id") val policyDecisionId: String,
    @SerializedName("reviewer_user_id") val reviewerUserId: String,
    @SerializedName("reviewer_role") val reviewerRole: String,
    val decision: String,
    val reason: String,
    val signature: String,
    @SerializedName("created_at") val createdAt: String? = null
)

data class AgentRunControlRequestDto(val reason: String = "")

data class AgentRunRetryRequestDto(
    val reason: String = "",
    @SerializedName("step_id") val stepId: String? = null
)

data class AgentRunExecuteRequestDto(
    @SerializedName("worker_id") val workerId: String = "android-mission-control",
    @SerializedName("lease_seconds") val leaseSeconds: Int = 300,
    @SerializedName("timeout_seconds") val timeoutSeconds: Int = 120
)
