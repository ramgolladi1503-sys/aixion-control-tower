package com.aixion.controltower.feature.auth

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.auth.AuthFailure
import com.aixion.controltower.data.repository.AuthRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

enum class AuthMessageSeverity(val accessibilityLabel: String) {
    INFO("Information"),
    WARNING("Warning"),
    ERROR("Error")
}

data class AuthUiState(
    val email: String = "",
    val password: String = "",
    val displayName: String = "",
    val inviteCode: String = "",
    val verificationCode: String = "",
    val privacyRemovalReason: String = "",
    val loading: Boolean = false,
    val authenticated: Boolean = false,
    val sessionChecked: Boolean = false,
    val verificationRequired: Boolean = false,
    val emailVerified: Boolean = false,
    val devVerificationCode: String? = null,
    val userLabel: String? = null,
    val message: String? = null,
    val messageSeverity: AuthMessageSeverity = AuthMessageSeverity.INFO
)

class AuthViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AuthRepository(
        api = ApiClient.create(application.applicationContext),
        context = application.applicationContext
    )

    private val _state = MutableStateFlow(AuthUiState())
    val state: StateFlow<AuthUiState> = _state.asStateFlow()

    init {
        refreshSession()
    }

    fun updateEmail(value: String) {
        _state.value = _state.value.copy(email = value)
    }

    fun updatePassword(value: String) {
        _state.value = _state.value.copy(password = value)
    }

    fun updateDisplayName(value: String) {
        _state.value = _state.value.copy(displayName = value)
    }

    fun updateInviteCode(value: String) {
        _state.value = _state.value.copy(inviteCode = value)
    }

    fun updateVerificationCode(value: String) {
        _state.value = _state.value.copy(verificationCode = value)
    }

    fun updatePrivacyRemovalReason(value: String) {
        _state.value = _state.value.copy(privacyRemovalReason = value)
    }

    fun login() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(
                loading = true,
                sessionChecked = false,
                message = null,
                messageSeverity = AuthMessageSeverity.INFO
            )
            runCatching {
                repository.login(current.email, current.password)
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    sessionChecked = true,
                    verificationRequired = false,
                    emailVerified = true,
                    userLabel = label,
                    message = "Logged in.",
                    messageSeverity = AuthMessageSeverity.INFO
                )
                syncPushRegistration()
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = "Login failed: ${AuthFailure.operatorMessage(error)}",
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun register() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(
                loading = true,
                sessionChecked = true,
                message = null,
                messageSeverity = AuthMessageSeverity.INFO
            )
            runCatching {
                repository.register(current.email, current.password, current.displayName, current.inviteCode)
            }.onSuccess { response ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    verificationRequired = response.verification_required,
                    emailVerified = response.user.email_verified,
                    devVerificationCode = response.dev_verification_code,
                    userLabel = listOf(response.user.display_name, response.user.email, response.user.role)
                        .filter { it.isNotBlank() }
                        .joinToString(" • "),
                    message = response.message,
                    messageSeverity = AuthMessageSeverity.INFO
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = "Registration failed: ${AuthFailure.operatorMessage(error)}",
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun verifyEmail() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null, messageSeverity = AuthMessageSeverity.INFO)
            runCatching {
                repository.verifyEmail(current.email, current.verificationCode)
            }.onSuccess { response ->
                _state.value = _state.value.copy(
                    loading = false,
                    verificationRequired = !response.verified,
                    emailVerified = response.user.email_verified,
                    devVerificationCode = null,
                    message = response.message,
                    messageSeverity = AuthMessageSeverity.INFO
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Verification failed: ${AuthFailure.operatorMessage(error)}",
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun resendVerification() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null, messageSeverity = AuthMessageSeverity.INFO)
            runCatching {
                repository.resendVerification(current.email)
            }.onSuccess { response ->
                _state.value = _state.value.copy(
                    loading = false,
                    verificationRequired = response.verification_required,
                    emailVerified = response.user.email_verified,
                    devVerificationCode = response.dev_verification_code,
                    message = response.message,
                    messageSeverity = AuthMessageSeverity.INFO
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Resend failed: ${AuthFailure.operatorMessage(error)}",
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun requestPrivacyAccountRemoval() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null, messageSeverity = AuthMessageSeverity.INFO)
            runCatching {
                repository.requestPrivacyAccountRemoval(current.privacyRemovalReason)
            }.onSuccess { response ->
                _state.value = AuthUiState(
                    sessionChecked = true,
                    message = "${response.message} Sign in again only if support reactivates access.",
                    messageSeverity = AuthMessageSeverity.INFO
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Privacy request failed: ${AuthFailure.operatorMessage(error)}",
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun refreshSession() {
        if (!repository.hasSavedToken()) {
            _state.value = _state.value.copy(
                loading = false,
                authenticated = false,
                sessionChecked = true,
                userLabel = null
            )
            return
        }
        viewModelScope.launch {
            _state.value = _state.value.copy(
                loading = true,
                authenticated = false,
                sessionChecked = false,
                message = "Verifying saved session...",
                messageSeverity = AuthMessageSeverity.INFO
            )
            runCatching {
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    sessionChecked = true,
                    emailVerified = true,
                    userLabel = label,
                    message = "Session verified.",
                    messageSeverity = AuthMessageSeverity.INFO
                )
                syncPushRegistration()
            }.onFailure { error ->
                repository.logout()
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = AuthFailure.operatorMessage(error),
                    messageSeverity = AuthMessageSeverity.ERROR
                )
            }
        }
    }

    fun logout() {
        if (_state.value.loading) return
        _state.value = _state.value.copy(loading = true, message = "Signing out…", messageSeverity = AuthMessageSeverity.INFO)
        viewModelScope.launch {
            val deviceCleanupSucceeded = try {
                repository.logout()
            } catch (cancelled: kotlinx.coroutines.CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                false
            }
            _state.value = AuthUiState(
                sessionChecked = true,
                message = if (deviceCleanupSucceeded) {
                    "Logged out. This device was removed from approval alerts and its server session was revoked."
                } else {
                    "Logged out locally. Server sign-out or approval-alert cleanup could not be confirmed."
                },
                messageSeverity = if (deviceCleanupSucceeded) AuthMessageSeverity.INFO else AuthMessageSeverity.WARNING
            )
        }
    }

    private fun syncPushRegistration() {
        viewModelScope.launch {
            repository.registerPushDeviceIfAllowed()
        }
    }
}
