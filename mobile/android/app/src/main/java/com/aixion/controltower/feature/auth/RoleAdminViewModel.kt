package com.aixion.controltower.feature.auth

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.data.repository.RoleAdminRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class RoleAdminUiState(
    val loading: Boolean = false,
    val updatingUserId: String? = null,
    val roles: List<String> = emptyList(),
    val users: List<AuthUserDto> = emptyList(),
    val message: String? = null,
    val errorMessage: String? = null
) {
    val canRenderOwnerPanel: Boolean
        get() = users.isNotEmpty() || loading || errorMessage != null || message != null
}

class RoleAdminViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = RoleAdminRepository(
        api = ApiClient.create(application.applicationContext)
    )

    private val _state = MutableStateFlow(RoleAdminUiState())
    val state: StateFlow<RoleAdminUiState> = _state.asStateFlow()

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = null, errorMessage = null)
            runCatching {
                val roles = repository.listRoles()
                val users = repository.listUsers()
                roles to users
            }.onSuccess { (roles, users) ->
                _state.value = _state.value.copy(
                    loading = false,
                    roles = roles,
                    users = users,
                    message = "Role admin refreshed."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = roleAdminError(error)
                )
            }
        }
    }

    fun updateRole(userId: String, role: String) {
        viewModelScope.launch {
            _state.value = _state.value.copy(
                updatingUserId = userId,
                message = null,
                errorMessage = null
            )
            runCatching {
                repository.updateRole(userId, role)
                repository.listUsers()
            }.onSuccess { users ->
                _state.value = _state.value.copy(
                    updatingUserId = null,
                    users = users,
                    message = "Role updated."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    updatingUserId = null,
                    errorMessage = roleAdminError(error)
                )
            }
        }
    }

    private fun roleAdminError(error: Throwable): String {
        val raw = error.message.orEmpty()
        return when {
            raw.contains("403") -> "Only OWNER can manage users."
            raw.contains("409") -> "Backend blocked the change, likely last OWNER protection."
            raw.contains("401") -> "Login session is missing or expired."
            raw.isNotBlank() -> raw
            else -> "Role admin request failed."
        }
    }
}
