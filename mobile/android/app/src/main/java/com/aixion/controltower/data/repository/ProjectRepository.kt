package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.ProjectSummary

class ProjectRepository(private val api: ControlTowerApi) {
    suspend fun listProjects(): List<ProjectSummary> {
        return api.listProjects().map { it.toUiSummary() }
    }
}