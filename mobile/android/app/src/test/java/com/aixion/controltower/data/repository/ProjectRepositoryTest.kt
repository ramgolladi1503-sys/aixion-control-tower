package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ProjectDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.fail
import org.junit.Test

class ProjectRepositoryTest {
    @Test
    fun listProjectsThrowsWhenBackendListFails() = runTest {
        val repository = ProjectRepository(FailingProjectApi())

        try {
            repository.listProjects()
        } catch (error: IllegalStateException) {
            return@runTest
        }

        fail("Expected IllegalStateException instead of mock project fallback")
    }
}

private class FailingProjectApi : BaseFakeControlTowerApi() {
    override suspend fun listProjects(): List<ProjectDto> {
        throw IllegalStateException("backend project list failed")
    }
}
