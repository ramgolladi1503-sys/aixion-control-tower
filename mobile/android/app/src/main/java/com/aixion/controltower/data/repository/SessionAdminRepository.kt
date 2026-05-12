package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.api.dto.SessionRevokeResponseDto

class SessionAdminRepository(
    private val api: ControlTowerApi
) {
    suspend fun listSessions(): List<SessionDto> = api.listSessions()

    suspend fun clearUserSessions(userId: String): SessionRevokeResponseDto = api.revokeUserSessions(userId)
}
