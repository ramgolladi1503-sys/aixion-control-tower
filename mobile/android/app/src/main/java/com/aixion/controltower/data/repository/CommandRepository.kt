package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.IdeaCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.WorkOrderSummary

class CommandRepository(private val api: ControlTowerApi) {
    suspend fun createControlledWorkOrder(
        project: ProjectSummary?,
        title: String,
        rawCommand: String
    ): WorkOrderSummary {
        val activeProject = project ?: api.listProjects().firstOrNull()?.toUiSummary()
            ?: error("No project available. Create or sync a project before creating a Work Order.")

        val idea = api.createIdea(
            IdeaCreateDto(
                project_id = activeProject.id,
                title = title.ifBlank { "Mobile command" },
                raw_text = rawCommand
            )
        )

        return api.createWorkOrder(
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
    }
}