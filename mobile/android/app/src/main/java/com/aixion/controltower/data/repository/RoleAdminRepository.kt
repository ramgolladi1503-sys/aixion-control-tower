package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.RoleUpdateRequestDto

class RoleAdminRepository(
    private val api: ControlTowerApi
) {
    suspend fun listRoles(): List<String> {
        return api.listRoleChoices().roles
    }

    suspend fun listUsers(): List<AuthUserDto> {
        return api.listUsers()
    }

    suspend fun updateRole(userId: String, role: String): AuthUserDto {
        return api.updateUserRole(userId, RoleUpdateRequestDto(role = role))
    }
}
