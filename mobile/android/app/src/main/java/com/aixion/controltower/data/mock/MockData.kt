package com.aixion.controltower.data.mock

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.AuditEventSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.model.WorkOrderSummary

object MockData {
    val projects = listOf(
        ProjectSummary(
            id = "project_tradebot",
            name = "Tradebot",
            description = "Options trading signal and execution control system.",
            mode = "STRICT",
            pendingApprovals = 2,
            blockedRequests = 1,
            healthScore = 62
        ),
        ProjectSummary(
            id = "project_mcp",
            name = "MCP Shield",
            description = "Policy, audit, and tool-call risk gateway.",
            mode = "STRICT",
            pendingApprovals = 1,
            blockedRequests = 0,
            healthScore = 78
        ),
        ProjectSummary(
            id = "project_jobs",
            name = "Job Application Agent",
            description = "Resume, cover letter, and recruiter-message preparation agent.",
            mode = "ASSISTED",
            pendingApprovals = 0,
            blockedRequests = 0,
            healthScore = 84
        )
    )

    val approvals = listOf(
        ApprovalSummary(
            id = "approval_critical",
            projectName = "Tradebot",
            title = "Update stale LTP execution guard",
            summary = "Blocks stale market data from becoming executable trades.",
            agentName = "builder-agent",
            targetBranch = "feature/stale-ltp-guard",
            status = ApprovalStatus.PENDING_REVIEW,
            risk = RiskLevel.CRITICAL,
            files = listOf(
                FileChangeSummary(
                    path = "core/execution_gate.py",
                    changeType = "update",
                    diff = "+ if stale_ltp:\n+     return ExecutionDecision(allowed=False, reason=\"STALE_LTP_BLOCK\")",
                    risk = RiskLevel.CRITICAL
                )
            ),
            requiredActions = listOf("Add rollback confirmation", "Run execution gate regression tests"),
            testPlan = listOf("pytest tests/test_execution_gate.py", "pytest tests/test_feed_freshness.py"),
            rollbackPlan = "Revert feature branch commit and rerun execution regression suite."
        ),
        ApprovalSummary(
            id = "approval_docs",
            projectName = "Aixion Control Tower",
            title = "Update architecture documentation",
            summary = "Adds mobile control tower architecture section.",
            agentName = "docs-agent",
            targetBranch = "feature/docs-architecture",
            status = ApprovalStatus.PENDING_REVIEW,
            risk = RiskLevel.LOW,
            files = listOf(
                FileChangeSummary(
                    path = "docs/ARCHITECTURE.md",
                    changeType = "update",
                    diff = "+ Add Android command center architecture section",
                    risk = RiskLevel.LOW
                )
            ),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert documentation commit."
        ),
        ApprovalSummary(
            id = "approval_blocked",
            projectName = "MCP Shield",
            title = "Attempt direct main branch edit",
            summary = "Agent tried to modify a protected branch.",
            agentName = "builder-agent",
            targetBranch = "main",
            status = ApprovalStatus.BLOCKED,
            risk = RiskLevel.BLOCKED,
            files = listOf(
                FileChangeSummary(
                    path = "backend/app/policies/evaluator.py",
                    changeType = "update",
                    diff = "+ update policy evaluator",
                    risk = RiskLevel.BLOCKED
                )
            ),
            requiredActions = listOf("Create a feature branch before requesting approval."),
            testPlan = listOf("pytest tests/test_policy_engine.py"),
            rollbackPlan = "Not applicable until moved to feature branch."
        )
    )

    val workOrders = listOf(
        WorkOrderSummary(
            id = "work_policy",
            projectName = "MCP Shield",
            goal = "Create policy engine MVP skeleton",
            risk = RiskLevel.HIGH,
            tasks = listOf("Create evaluator", "Create risk scorer", "Add audit tests"),
            requiredTests = listOf("pytest tests/test_policy_engine.py")
        ),
        WorkOrderSummary(
            id = "work_mobile",
            projectName = "Aixion Control Tower",
            goal = "Build mobile approval inbox and diff viewer",
            risk = RiskLevel.MEDIUM,
            tasks = listOf("Create inbox screen", "Create detail screen", "Create diff block"),
            requiredTests = listOf("UI smoke test later")
        )
    )

    val auditEvents = listOf(
        AuditEventSummary("audit_1", "approval.created", "system", "Tradebot critical approval created", "04:01"),
        AuditEventSummary("audit_2", "risk.blocked", "risk-engine", "Main branch edit blocked", "04:02"),
        AuditEventSummary("audit_3", "work_order.created", "system", "MCP Shield policy work order created", "04:03")
    )
}
