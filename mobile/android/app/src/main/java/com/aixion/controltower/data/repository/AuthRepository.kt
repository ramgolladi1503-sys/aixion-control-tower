package com.aixion.controltower.data.repository

import android.content.Context
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.auth.AuthSession

class AuthRepository(
    private val api: ControlTowerApi,
    private val context: Context
) {
    suspend fun login(email: String, password: String) {
        val response = api.login(LoginRequestDto(email = email.trim(), password = password))
        AuthSession.saveAccessToken(context, response.access_token)
    }

    suspend fun register(email: String, password: String, displayName: String) {
        val response = api.register(
            RegisterRequestDto(
                email = email.trim(),
                password = password,
                display_name = displayName.trim()
            )
        )
        AuthSession.saveAccessToken(context, response.access_token)
    }

    suspend fun currentUserLabel(): String {
        val user = api.me()
        return listOf(user.display_name, user.email, user.role)
            .filter { it.isNotBlank() }
            .joinToString(" • ")
    }

    fun hasSavedToken(): Boolean = AuthSession.getAccessToken(context).isNotBlank()

    fun logout() {
        AuthSession.clear(context)
    }
}
