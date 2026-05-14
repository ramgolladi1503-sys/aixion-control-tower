package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.OpsApi
import com.aixion.controltower.core.model.RuntimeReadinessSummary

class OpsRepository(
    private val api: OpsApi
) {
    suspend fun getRuntimeReadiness(): RuntimeReadinessSummary {
        return try {
            api.getRuntimeReadiness().toUiSummary()
        } catch (error: Exception) {
            throw IllegalStateException("Unable to load runtime readiness", error)
        }
    }
}
