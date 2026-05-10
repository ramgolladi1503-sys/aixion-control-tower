package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.AuditEventDto
import com.aixion.controltower.core.api.dto.FileChangeDto
import com.aixion.controltower.core.api.dto.MCPPendingHealthDto
import com.aixion.controltower.core.api.dto.MCPPendingRequestDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.TestRunDto
import com.aixion.controltower.core.api.dto.WorkOrderDto
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.AuditEventSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.MCPPendingHealthSummary
import com.aixion.controltower.core.model.MCPPendingStatus
import com.aixion.controltower.core.model.MCPPendingSummary
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.model.TestRunSummary
import com.aixion.controltower.core.model.WorkOrderSummary

fun String.toRiskLevelOrDefault(): RiskLevel = runCatching {
    RiskLevel.valueOf(uppercase())
}.getOrDefault(RiskLevel.MEDIUM)

fun String.toApprovalStatusOrDefault(): ApprovalStatus {
    val normalized = uppercase()
    val canonical = when (normalized) {
        "PENDING_REVIEW" -> "REQUESTED"
        "REJECTED" -> "DENIED"
        "APPLIED", "TESTS_RUNNING" -> "EXECUTING"
        "TESTS_FAILED" -> "FAILED"
        "TESTS_PASSED" -> "MERGED"
        else -> normalized
    }
    return runCatching {
        ApprovalStatus.valueOf(canonical)
    }.getOrDefault(ApprovalStatus.REQUESTED)
}

fun String.toMCPPendingStatusOrDefault(): MCPPendingStatus {
    return runCatching {
        MCPPendingStatus.valueOf(uppercase())
    }.getOrDefault(MCPPendingStatus.WAITING_FOR_APPROVAL)
}

fun ProjectDto.toUiSummary(
    pendingApprovals: Int = 0,
    blockedRequests: Int = 0,
    healthScore: Int = 75
): ProjectSummary {
    return ProjectSummary(
        id = id,
        name = name,
        description = description.orEmpty(),
        mode = mode ?: "STRICT",
        pendingApprovals = pendingApprovals,
        blockedRequests = blockedRequests,
        healthScore = healthScore
    )
}

fun FileChangeDto.toUiSummary(fallbackRisk: RiskLevel): FileChangeSummary {
    return FileChangeSummary(
        path = path,
        changeType = change_type,
        diff = diff,
        risk = fallbackRisk
    )
}

fun ApprovalRequestDto.toUiSummary(projectName: String = "Project"): ApprovalSummary {
    val riskLevel = risk.level.toRiskLevelOrDefault()
    return ApprovalSummary(
        id = id,
        projectName = projectName,
        title = title,
        summary = summary,
        agentName = agent_name,
        targetBranch = target_branch,
        status = status.toApprovalStatusOrDefault(),
        risk = riskLevel,
        files = files.map { it.toUiSummary(riskLevel) },
        requiredActions = risk.required_actions,
        testPlan = test_plan,
        rollbackPlan = rollback_plan.orEmpty(),
        sourceProvider = uiSourceProvider,
        sourceAgentId = uiSourceAgentId,
        sourceAgentName = uiSourceAgentName,
        sourceSessionId = uiSourceSessionId,
        sourceTaskUrl = uiSourceTaskUrl,
        createdByUserId = uiCreatedByUserId,
        verifiedSource = uiVerifiedSource
    )
}

fun WorkOrderDto.toUiSummary(projectName: String = "Project"): WorkOrderSummary {
    return WorkOrderSummary(
        id = id,
        projectName = projectName,
        goal = goal,
        risk = (risk_level ?: "MEDIUM").toRiskLevelOrDefault(),
        tasks = tasks,
        requiredTests = required_tests
    )
}

fun TestRunDto.toUiSummary(): TestRunSummary {
    return TestRunSummary(
        id = id,
        approvalRequestId = approval_request_id,
        command = command,
        status = status,
        outputSummary = output_summary.orEmpty()
    )
}

fun AuditEventDto.toUiSummary(): AuditEventSummary {
    val detailsText = details.entries.joinToString(" • ") { (key, value) -> "$key=$value" }
    return AuditEventSummary(
        id = id,
        eventType = event_type,
        actor = actor,
        details = detailsText.ifBlank { entity_id },
        timestamp = created_at ?: "recent"
    )
}

fun MCPPendingRequestDto.toUiSummary(): MCPPendingSummary {
    return MCPPendingSummary(
        id = id,
        projectId = project_id,
        approvalRequestId = approval_request_id,
        serverName = server_name,
        toolName = tool_name,
        requestedBy = requested_by,
        status = status.toMCPPendingStatusOrDefault(),
        attempts = attempts,
        maxAttempts = max_attempts,
        leaseOwner = lease_owner,
        leaseExpiresAt = lease_expires_at,
        lastError = last_error,
        createdAt = created_at,
        updatedAt = updated_at
    )
}

fun MCPPendingHealthDto.toUiSummary(): MCPPendingHealthSummary {
    return MCPPendingHealthSummary(
        total = total,
        byStatus = by_status,
        waitingForApproval = waiting_for_approval,
        forwarding = forwarding,
        activeLeases = active_leases,
        expiredLeases = expired_leases,
        retryable = retryable,
        deadLetter = dead_letter,
        terminal = terminal,
        attentionRequired = attention_required,
        oldestWaitingCreatedAt = oldest_waiting_created_at,
        oldestDeadLetterCreatedAt = oldest_dead_letter_created_at
    )
}
