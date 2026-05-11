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
    val loading: Boolean = false,
    val authenticated: Boolean = false,
    val userLabel: String? = null,
    val message: String? = null
)

class AuthViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AuthRepository(
        api = ApiClient.create(application.applicationContext),
        context = application.applicationContext
    )

    private val _state = MutableStateFlow(AuthUiState(authenticated = repository.hasSavedToken()))
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

    fun login() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null)
            runCatching {
                repository.login(current.email, current.password)
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    userLabel = label,
                    message = "Logged in."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = repository.hasSavedToken(),
                    message = "Login failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun register() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, message = null)
            runCatching {
                repository.register(current.email, current.password, current.displayName, current.inviteCode)
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    userLabel = label,
                    message = "Registered and logged in."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = repository.hasSavedToken(),
                    message = "Registration failed: ${AuthFailure.operatorMessage(error)}"
                )
            }
        }
    }

    fun refreshSession() {
        if (!repository.hasSavedToken()) {
            _state.value = _state.value.copy(authenticated = false, userLabel = null)
            return
        }
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = null)
            runCatching {
                repository.currentUserLabel()
            }.onSuccess { label ->
                _state.value = _state.value.copy(
                    loading = false,
                    authenticated = true,
                    userLabel = label,
                    message = "Session verified."
                )
            }.onFailure { error ->
                if (AuthFailure.shouldClearSavedSession(error)) {
                    repository.logout()
                    _state.value = _state.value.copy(
                        loading = false,
                        authenticated = false,
                        userLabel = null,
                        message = AuthFailure.operatorMessage(error)
                    )
                } else {
                    _state.value = _state.value.copy(
                        loading = false,
                        authenticated = repository.hasSavedToken(),
                        message = "Saved token found, but session check failed: ${AuthFailure.operatorMessage(error)}"
                    )
                }
            }
        }
    }

    fun logout() {
        repository.logout()
        _state.value = AuthUiState(message = "Logged out.")
    }
}
