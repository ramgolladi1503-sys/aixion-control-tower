package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.TestRunSummary

class TestRunRepository(private val api: ControlTowerApi) {
    suspend fun listTestRuns(): List<TestRunSummary> {
        return runCatching {
            api.listTestRuns().map { it.toUiSummary() }
        }.getOrElse {
            emptyList()
        }
    }
}
