package com.aixion.controltower.feature.auth

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.InviteDto
import com.aixion.controltower.data.repository.InviteAdminRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class InviteAdminUiState(
    val loading: Boolean = false,
    val inviteEmail: String = "",
    val selectedRole: String = "REVIEWER",
    val invites: List<InviteDto> = emptyList(),
    val latestInviteCode: String? = null,
    val message: String? = null,
    val errorMessage: String? = null
)

class InviteAdminViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = InviteAdminRepository(
        api = ApiClient.create(application.applicationContext)
    )

    private val _state = MutableStateFlow(InviteAdminUiState())
    val state: StateFlow<InviteAdminUiState> = _state.asStateFlow()

    fun updateInviteEmail(value: String) {
        _state.value = _state.value.copy(inviteEmail = value)
    }

    fun updateSelectedRole(value: String) {
        _state.value = _state.value.copy(selectedRole = value)
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = null, errorMessage = null)
            runCatching {
                repository.listInvites()
            }.onSuccess { invites ->
                _state.value = _state.value.copy(
                    loading = false,
                    invites = invites,
                    message = "Invites refreshed."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = inviteAdminError(error)
                )
            }
        }
    }

    fun createInvite() {
        val current = _state.value
        viewModelScope.launch {
            _state.value = current.copy(loading = true, latestInviteCode = null, message = null, errorMessage = null)
            runCatching {
                val created = repository.createInvite(current.inviteEmail, current.selectedRole)
                val invites = repository.listInvites()
                created to invites
            }.onSuccess { (created, invites) ->
                _state.value = _state.value.copy(
                    loading = false,
                    inviteEmail = "",
                    invites = invites,
                    latestInviteCode = created.token,
                    message = "Invite created. Copy this code now; it is shown only once."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = inviteAdminError(error)
                )
            }
        }
    }

    fun revokeInvite(inviteId: String) {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = null, errorMessage = null)
            runCatching {
                repository.revokeInvite(inviteId)
                repository.listInvites()
            }.onSuccess { invites ->
                _state.value = _state.value.copy(
                    loading = false,
                    invites = invites,
                    message = "Invite revoked."
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = inviteAdminError(error)
                )
            }
        }
    }

    private fun inviteAdminError(error: Throwable): String {
        val raw = error.message.orEmpty()
        return when {
            raw.contains("401") -> "Login session is missing or expired."
            raw.contains("403") -> "Only OWNER can manage invites."
            raw.contains("409") -> "Backend blocked the invite action, likely duplicate, accepted, revoked, or expired invite."
            raw.isNotBlank() -> raw
            else -> "Invite admin request failed."
        }
    }
}
