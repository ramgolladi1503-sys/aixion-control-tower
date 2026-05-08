package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.data.mock.MockData

class ProjectRepository(private val api: ControlTowerApi) {
    suspend fun listProjects(): List<ProjectSummary> {
        return runCatching {
            api.listProjects().map { it.toUiSummary() }
        }.getOrElse {
            MockData.projects
        }
    }
}
