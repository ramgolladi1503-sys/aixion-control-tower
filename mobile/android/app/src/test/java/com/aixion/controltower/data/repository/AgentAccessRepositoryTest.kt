package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ProjectCreateDto
import com.aixion.controltower.core.api.dto.ProjectDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class AgentAccessRepositoryTest {
    @Test
    fun createProjectTrimsNameAndDescriptionAndUsesDefaultStrictMode() = runTest {
        var received: ProjectCreateDto? = null
        val repository = AgentAccessRepository(object : BaseFakeControlTowerApi() {
            override suspend fun createProject(payload: ProjectCreateDto): ProjectDto {
                received = payload
                return ProjectDto(id = "project-1", name = payload.name, description = payload.description)
            }
        })

        val created = repository.createProject("  My project  ", "  Isolated workspace  ")

        assertEquals("project-1", created.id)
        assertEquals(ProjectCreateDto(name = "My project", description = "Isolated workspace"), received)
    }

    @Test(expected = IllegalArgumentException::class)
    fun createProjectRejectsBlankNameBeforeRequest() = runTest {
        AgentAccessRepository(object : BaseFakeControlTowerApi() {})
            .createProject("   ", "optional")
    }
}
