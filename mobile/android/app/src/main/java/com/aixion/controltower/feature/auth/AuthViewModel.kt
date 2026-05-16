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
    val message: String? = null
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
            _state.value = current.copy(loading = true, sessionChecked = false, message = null)
            runCatching {
                repository.login(current.email, current.password)
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    sessionChecked = true,
                    verificationRequired = false,
                    emailVerified = true,
                    userLabel = label,
                    message = "Logged in."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = "Login failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun register() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, sessionChecked = true, message = null)
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
                    message = response.message
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = "Registration failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun verifyEmail() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null)
            runCatching {
                repository.verifyEmail(current.email, current.verificationCode)
            }.onSuccess { response ->
                _state.value = _state.value.copy(
                    loading = false,
                    verificationRequired = !response.verified,
                    emailVerified = response.user.email_verified,
                    devVerificationCode = null,
                    message = response.message
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Verification failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun resendVerification() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null)
            runCatching {
                repository.resendVerification(current.email)
            }.onSuccess { response ->
                _state.value = _state.value.copy(
                    loading = false,
                    verificationRequired = response.verification_required,
                    emailVerified = response.user.email_verified,
                    devVerificationCode = response.dev_verification_code,
                    message = response.message
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Resend failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun requestPrivacyAccountRemoval() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null)
            runCatching {
                repository.requestPrivacyAccountRemoval(current.privacyRemovalReason)
            }.onSuccess { response ->
                _state.value = AuthUiState(
                    sessionChecked = true,
                    message = "${response.message} Sign in again only if support reactivates access."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    message = "Privacy request failed: ${AuthFailure.operatorMessage(error)}"
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
            _state.value = _state.value.copy(loading = true, authenticated = false, sessionChecked = false, message = "Verifying saved session...")
            runCatching {
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    sessionChecked = true,
                    emailVerified = true,
                    userLabel = label,
                    message = "Session verified."
                )
            }.onFailure { error ->
                repository.logout()
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = false,
                    sessionChecked = true,
                    userLabel = null,
                    message = AuthFailure.operatorMessage(error)
                )
            }
        }
    }

    fun logout() {
        repository.logout()
        _state.value = AuthUiState(sessionChecked = true, message = "Logged out.")
    }
}