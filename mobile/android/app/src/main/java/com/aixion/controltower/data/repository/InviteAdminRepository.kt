package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.InviteCreateRequestDto
import com.aixion.controltower.core.api.dto.InviteCreateResponseDto
import com.aixion.controltower.core.api.dto.InviteDto

class InviteAdminRepository(
    private val api: ControlTowerApi
) {
    suspend fun createInvite(email: String, role: String, expiresInDays: Int = 7): InviteCreateResponseDto {
        return api.createInvite(
            InviteCreateRequestDto(
                email = email.trim(),
                role = role,
                expires_in_days = expiresInDays
            )
        )
    }

    suspend fun listInvites(): List<InviteDto> {
        return api.listInvites()
    }

    suspend fun revokeInvite(inviteId: String): InviteDto {
        return api.revokeInvite(inviteId)
    }
}
