package com.aixion.controltower.data.repository

import android.content.Context
import android.util.Log
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.AccountDeletionRequestDto
import com.aixion.controltower.core.api.dto.AccountDeletionResponseDto
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.api.dto.RegistrationResponseDto
import com.aixion.controltower.core.api.dto.ResendVerificationRequestDto
import com.aixion.controltower.core.api.dto.VerifyEmailRequestDto
import com.aixion.controltower.core.api.dto.VerifyEmailResponseDto
import com.aixion.controltower.core.auth.AuthSession
import com.aixion.controltower.notifications.PushDeviceRegistrationRepository
import com.aixion.controltower.notifications.PushRegistrationResult
import kotlinx.coroutines.CancellationException

internal suspend fun <T> establishAuthenticatedSession(
    token: String,
    saveToken: (String) -> Unit,
    validateSession: suspend () -> T,
    clearToken: () -> Unit
): T {
    try {
        saveToken(token)
        return validateSession()
    } catch (failure: Throwable) {
        try {
            clearToken()
        } catch (cleanupFailure: Throwable) {
            failure.addSuppressed(cleanupFailure)
        }
        throw failure
    }
}

internal suspend fun logoutWithCleanup(
    unregisterPushDevice: suspend () -> Boolean,
    revokeServerSession: suspend () -> Unit,
    clearLocalSession: () -> Unit,
    onFailure: (stage: String, error: Exception) -> Unit
): Boolean {
    var cleanupSucceeded = true
    try {
        try {
            if (!unregisterPushDevice()) cleanupSucceeded = false
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            cleanupSucceeded = false
            onFailure("push-device-unregister", error)
        }
        try {
            revokeServerSession()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            cleanupSucceeded = false
            onFailure("server-session-revocation", error)
        }
    } finally {
        clearLocalSession()
    }
    return cleanupSucceeded
}

class AuthRepository(
    private val api: ControlTowerApi,
    private val context: Context
) {
    private val pushRegistrationRepository = PushDeviceRegistrationRepository(context)

    suspend fun login(email: String, password: String): String {
        val response = api.login(LoginRequestDto(email = email.trim(), password = password))
        val label = establishAuthenticatedSession(
            token = response.access_token,
            saveToken = { AuthSession.saveAccessToken(context, it) },
            validateSession = {
                val user = api.me()
                listOf(user.display_name, user.email, user.role)
                    .filter { it.isNotBlank() }
                    .joinToString(" • ")
            },
            clearToken = { AuthSession.clear(context) }
        )
        try {
            pushRegistrationRepository.retryPendingUnregistrations()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            Log.w("AuthRepository", "Post-login push cleanup could not complete.", error)
        }
        return label
    }

    suspend fun register(email: String, password: String, displayName: String, inviteCode: String = ""): RegistrationResponseDto {
        return api.register(
            RegisterRequestDto(
                email = email.trim(),
                password = password,
                display_name = displayName.trim(),
                invite_token = inviteCode.trim().ifBlank { null }
            )
        )
    }

    suspend fun verifyEmail(email: String, code: String): VerifyEmailResponseDto {
        return api.verifyEmail(VerifyEmailRequestDto(email = email.trim(), code = code.trim()))
    }

    suspend fun resendVerification(email: String): RegistrationResponseDto {
        return api.resendVerification(ResendVerificationRequestDto(email = email.trim()))
    }

    suspend fun currentUserLabel(): String {
        val user = api.me()
        return listOf(user.display_name, user.email, user.role)
            .filter { it.isNotBlank() }
            .joinToString(" • ")
    }

    internal suspend fun registerPushDeviceIfAllowed(): PushRegistrationResult =
        pushRegistrationRepository.registerCurrentDevice()

    suspend fun requestPrivacyAccountRemoval(reason: String): AccountDeletionResponseDto {
        val response = api.requestAccountDeletion(AccountDeletionRequestDto(reason = reason.trim()))
        AuthSession.clear(context)
        return response
    }

    fun hasSavedToken(): Boolean = AuthSession.getAccessToken(context).isNotBlank()

    suspend fun logout(): Boolean {
        return logoutWithCleanup(
            unregisterPushDevice = { pushRegistrationRepository.unregisterCurrentDevice() },
            revokeServerSession = { api.logout() },
            clearLocalSession = { AuthSession.clear(context) },
            onFailure = { stage, error ->
                Log.w("AuthRepository", "Logout cleanup stage failed: $stage", error)
            }
        )
    }
}
