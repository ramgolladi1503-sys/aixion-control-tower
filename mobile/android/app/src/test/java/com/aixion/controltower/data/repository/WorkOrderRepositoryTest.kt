package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.WorkOrderDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.fail
import org.junit.Test

class WorkOrderRepositoryTest {
    @Test
    fun listWorkOrdersThrowsWhenBackendListFails() = runTest {
        val repository = WorkOrderRepository(FailingWorkOrderApi())

        try {
            repository.listWorkOrders()
        } catch (error: IllegalStateException) {
            return@runTest
        }

        fail("Expected IllegalStateException instead of mock work-order fallback")
    }
}

private class FailingWorkOrderApi : BaseFakeControlTowerApi() {
    override suspend fun listWorkOrders(): List<WorkOrderDto> {
        throw IllegalStateException("backend work-order list failed")
    }
}
