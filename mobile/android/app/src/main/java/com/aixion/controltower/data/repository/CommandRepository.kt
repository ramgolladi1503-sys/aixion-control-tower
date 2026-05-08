package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.IdeaCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.model.WorkOrderSummary
import com.aixion.controltower.data.mock.MockData

class CommandRepository(private val api: ControlTowerApi) {
    suspend fun createControlledWorkOrder(
        project: ProjectSummary?,
        title: String,
        rawCommand: String
    ): WorkOrderSummary {
        return runCatching {
            val activeProject = project ?: api.listProjects().firstOrNull()?.toUiSummary()
                ?: error("No project available")

            val idea = api.createIdea(
                IdeaCreateDto(
                    project_id = activeProject.id,
                    title = title.ifBlank { "Mobile command" },
                    raw_text = rawCommand
                )
            )

            api.createWorkOrder(
                WorkOrderCreateDto(
                    project_id = activeProject.id,
                    idea_id = idea.id,
                    goal = rawCommand,
                    context = "Generated from Android Command screen.",
                    tasks = listOf(
                        "Analyze requested change",
                        "Identify affected files",
                        "Prepare safe implementation plan",
                        "Add test and rollback requirements"
                    ),
                    affected_areas = listOf("project planning", "agent work order"),
                    required_tests = listOf("Define tests before approval"),
                    rollback_plan = "No code is applied at work-order stage. Reject or revise before implementation."
                )
            ).toUiSummary(activeProject.name)
        }.getOrElse {
            WorkOrderSummary(
                id = "offline_work_order",
                projectName = project?.name ?: MockData.projects.first().name,
                goal = rawCommand,
                risk = RiskLevel.MEDIUM,
                tasks = listOf(
                    "Offline fallback: backend unavailable",
                    "Review idea later",
                    "Create backend work order when service is running"
                ),
                requiredTests = listOf("Backend connection required")
            )
        }
    }
}
