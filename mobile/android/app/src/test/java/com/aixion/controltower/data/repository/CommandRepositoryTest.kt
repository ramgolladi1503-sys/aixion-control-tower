package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.IdeaCreateDto
import com.aixion.controltower.core.api.dto.IdeaDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderDto
import com.aixion.controltower.core.model.ProjectSummary
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class CommandRepositoryTest {
    @Test
    fun createControlledWorkOrderFailsWhenNoProjectExistsAndDoesNotCreateOfflineWorkOrder() = runTest {
        val api = EmptyProjectCommandApi()
        val repository = CommandRepository(api)

        try {
            repository.createControlledWorkOrder(
                project = null,
                title = "Improve login flow",
                rawCommand = "Prepare a safe Work Order for login validation"
            )
        } catch (error: IllegalStateException) {
            assertEquals("No project available. Create or sync a project before creating a Work Order.", error.message)
            assertFalse(api.createIdeaCalled)
            assertFalse(api.createWorkOrderCalled)
            return@runTest
        }

        fail("Expected CommandRepository to fail instead of creating an offline Work Order")
    }

    @Test
    fun createControlledWorkOrderFailsWhenProjectSyncFailsAndDoesNotCreateOfflineWorkOrder() = runTest {
        val api = FailingProjectCommandApi()
        val repository = CommandRepository(api)

        try {
            repository.createControlledWorkOrder(
                project = null,
                title = "Improve retry flow",
                rawCommand = "Prepare retry work package"
            )
        } catch (error: IllegalStateException) {
            assertEquals("backend project list failed", error.message)
            assertFalse(api.createIdeaCalled)
            assertFalse(api.createWorkOrderCalled)
            return@runTest
        }

        fail("Expected backend project failure to stop Command creation")
    }

    @Test
    fun createControlledWorkOrderPropagatesWorkOrderCreateFailureWithoutFallback() = runTest {
        val project = projectFixture()
        val api = FailingCreateWorkOrderCommandApi()
        val repository = CommandRepository(api)

        try {
            repository.createControlledWorkOrder(
                project = project,
                title = "Improve approval UX",
                rawCommand = "Prepare approval UX Work Order"
            )
        } catch (error: IllegalStateException) {
            assertEquals("backend work order create failed", error.message)
            assertTrue(api.createIdeaCalled)
            assertTrue(api.createWorkOrderCalled)
            assertEquals("project_1", api.lastWorkOrderPayload?.project_id)
            assertEquals("idea_1", api.lastWorkOrderPayload?.idea_id)
            assertEquals("Prepare approval UX Work Order", api.lastWorkOrderPayload?.goal)
            return@runTest
        }

        fail("Expected createWorkOrder failure to propagate instead of returning offline_work_order")
    }

    @Test
    fun createControlledWorkOrderUsesBackendWorkOrderIdWhenSuccessful() = runTest {
        val project = projectFixture()
        val api = SuccessfulCommandApi()
        val repository = CommandRepository(api)

        val workOrder = repository.createControlledWorkOrder(
            project = project,
            title = "Improve approval UX",
            rawCommand = "Prepare approval UX Work Order"
        )

        assertEquals("backend_work_order_1", workOrder.id)
        assertEquals("Aixion", workOrder.projectName)
        assertEquals("Prepare approval UX Work Order", workOrder.goal)
        assertFalse(workOrder.id.startsWith("offline"))
        assertTrue(api.createIdeaCalled)
        assertTrue(api.createWorkOrderCalled)
    }

    private fun projectFixture(): ProjectSummary = ProjectSummary(
        id = "project_1",
        name = "Aixion",
        description = "demo",
        mode = "STRICT",
        pendingApprovals = 0,
        blockedRequests = 0,
        healthScore = 100
    )
}

private open class CommandApiBase : BaseFakeControlTowerApi() {
    var createIdeaCalled = false
    var createWorkOrderCalled = false
    var lastWorkOrderPayload: WorkOrderCreateDto? = null

    override suspend fun createIdea(payload: IdeaCreateDto): IdeaDto {
        createIdeaCalled = true
        return IdeaDto(
            id = "idea_1",
            project_id = payload.project_id,
            title = payload.title,
            raw_text = payload.raw_text
        )
    }

    override suspend fun createWorkOrder(payload: WorkOrderCreateDto): WorkOrderDto {
        createWorkOrderCalled = true
        lastWorkOrderPayload = payload
        return WorkOrderDto(
            id = "backend_work_order_1",
            project_id = payload.project_id,
            idea_id = payload.idea_id,
            goal = payload.goal,
            context = payload.context,
            tasks = payload.tasks,
            affected_areas = payload.affected_areas,
            required_tests = payload.required_tests,
            rollback_plan = payload.rollback_plan,
            risk_level = "MEDIUM"
        )
    }
}

private class EmptyProjectCommandApi : CommandApiBase() {
    override suspend fun listProjects(): List<ProjectDto> = emptyList()
}

private class FailingProjectCommandApi : CommandApiBase() {
    override suspend fun listProjects(): List<ProjectDto> {
        throw IllegalStateException("backend project list failed")
    }
}

private class FailingCreateWorkOrderCommandApi : CommandApiBase() {
    override suspend fun createWorkOrder(payload: WorkOrderCreateDto): WorkOrderDto {
        createWorkOrderCalled = true
        lastWorkOrderPayload = payload
        throw IllegalStateException("backend work order create failed")
    }
}

private class SuccessfulCommandApi : CommandApiBase()
