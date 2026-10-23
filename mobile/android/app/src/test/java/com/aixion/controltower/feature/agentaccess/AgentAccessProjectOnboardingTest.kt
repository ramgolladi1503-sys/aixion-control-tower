package com.aixion.controltower.feature.agentaccess

import android.app.Application
import android.content.Context
import androidx.lifecycle.SavedStateHandle
import com.aixion.controltower.core.api.dto.ProjectCreateDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.data.repository.AgentAccessRepository
import com.aixion.controltower.data.repository.BaseFakeControlTowerApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class AgentAccessProjectOnboardingTest {
    @Test
    fun emptyAuthenticatedAccountCanCreateStrictProjectAndContinueConnectorSetup() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = ProjectOnboardingApi()
            val viewModel = AgentAccessViewModel(
                newApplication(),
                SavedStateHandle(mapOf("provider" to "ANTIGRAVITY")),
                AgentAccessRepository(api)
            )
            runCurrent()

            assertTrue(viewModel.state.value.projects.isEmpty())
            assertNull(viewModel.state.value.selectedProjectId)
            assertFalse(canCreateApprovalAgent(viewModel.state.value))

            viewModel.setNewProjectName("  Approval pilot  ")
            viewModel.createProject()
            runCurrent()

            val created = viewModel.state.value.projects.single()
            assertEquals("Approval pilot", created.name)
            assertEquals("project-new", viewModel.state.value.selectedProjectId)
            assertEquals("STRICT", api.createdPayload?.mode)
            assertEquals("Approval pilot", api.createdPayload?.name)
            assertTrue(canCreateApprovalAgent(viewModel.state.value))
            assertTrue(viewModel.state.value.error.isBlank())
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun serverResponseThatDoesNotConfirmStrictProjectKeepsConnectorSetupDisabled() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = ProjectOnboardingApi().apply {
                createResponse = ProjectDto(id = "", name = "Approval pilot", mode = "OPEN")
            }
            val viewModel = AgentAccessViewModel(
                newApplication(),
                SavedStateHandle(mapOf("provider" to "ANTIGRAVITY")),
                AgentAccessRepository(api)
            )
            runCurrent()

            viewModel.setNewProjectName("Approval pilot")
            viewModel.createProject()
            runCurrent()

            assertTrue(viewModel.state.value.projects.isEmpty())
            assertNull(viewModel.state.value.selectedProjectId)
            assertFalse(canCreateApprovalAgent(viewModel.state.value))
            assertTrue(viewModel.state.value.error.isNotBlank())
            assertTrue(viewModel.state.value.projectCreationNeedsRefresh)

            viewModel.setNewProjectName("Retry project")
            viewModel.createProject()
            runCurrent()
            assertEquals("A retry must wait for project-list reconciliation.", 1, api.createCalls)
        } finally {
            Dispatchers.resetMain()
        }
    }

    private class ProjectOnboardingApi : BaseFakeControlTowerApi() {
        var createdPayload: ProjectCreateDto? = null
        var createCalls = 0
        var createResponse = ProjectDto(
            id = "project-new",
            name = "Approval pilot",
            mode = "STRICT"
        )
        private val projects = mutableListOf<ProjectDto>()

        override suspend fun listProjects(): List<ProjectDto> = projects.toList()

        override suspend fun createProject(payload: ProjectCreateDto): ProjectDto {
            createCalls += 1
            createdPayload = payload
            projects += createResponse
            return createResponse
        }
    }

    private class TestApplication : Application() {
        override fun getApplicationContext(): Context = this
    }

    private fun newApplication(): Application = unsafe.javaClass
        .getMethod("allocateInstance", Class::class.java)
        .invoke(unsafe, TestApplication::class.java) as Application

    private companion object {
        val unsafe: Any = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe").run {
            isAccessible = true
            requireNotNull(get(null))
        }
    }
}
