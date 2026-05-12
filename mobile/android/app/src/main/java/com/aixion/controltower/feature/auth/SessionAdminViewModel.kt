package com.aixion.controltower.feature.auth

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.data.repository.SessionAdminRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class SessionAdminUiState(
    val loading: Boolean = false,
    val clearingUserId: String? = null,
    val sessions: List<SessionDto> = emptyList(),
    val message: String? = null,
    val errorMessage: String? = null
)

class SessionAdminViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = SessionAdminRepository(
        api = ApiClient.create(application.applicationContext)
    )

    private val _state = MutableStateFlow(SessionAdminUiState())
    val state: StateFlow<SessionAdminUiState> = _state.asStateFlow()

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = null, errorMessage = null)
            runCatching {
                repository.listSessions()
            }.onSuccess { sessions ->
                _state.value = _state.value.copy(
                    loading = false,
                    sessions = sessions,
                    message = "Sessions refreshed."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = sessionAdminError(error)
                )
            }
        }
    }

    fun clearUserSessions(userId: String) {
        viewModelScope.launch {
            _state.value = _state.value.copy(
                loading = true,
                clearingUserId = userId,
                message = null,
                errorMessage = null
            )
            runCatching {
                val result = repository.clearUserSessions(userId)
                val sessions = repository.listSessions()
                result to sessions
            }.onSuccess { (result, sessions) ->
                _state.value = _state.value.copy(
                    loading = false,
                    clearingUserId = null,
                    sessions = sessions,
                    message = "Cleared ${result.revoked_sessions_count} active session(s) for ${result.target_email}."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    clearingUserId = null,
                    errorMessage = sessionAdminError(error)
                )
            }
        }
    }

    private fun sessionAdminError(error: Throwable): String {
        val raw = error.message.orEmpty()
        return when {
            raw.contains("401") -> "Login session is missing or expired."
            raw.contains("403") -> "Only OWNER can manage sessions."
            raw.contains("404") -> "Target user was not found."
            raw.contains("409") -> "Backend blocked this session action, likely self-session protection."
            raw.isNotBlank() -> raw
            else -> "Session admin request failed."
        }
    }
}
