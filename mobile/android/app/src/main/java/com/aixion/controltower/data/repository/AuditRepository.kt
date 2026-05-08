package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.AuditEventSummary
import com.aixion.controltower.data.mock.MockData

class AuditRepository(private val api: ControlTowerApi) {
    suspend fun listAuditEvents(): List<AuditEventSummary> {
        return runCatching {
            api.listAuditEvents().map { it.toUiSummary() }
        }.getOrElse {
            MockData.auditEvents
        }
    }
}
