package com.aixion.controltower.core.api.dto

data class AuthUserDto(
    val id: String,
    val email: String,
    val display_name: String,
    val role: String,
    val email_verified: Boolean = false
)

data class LoginRequestDto(
    val email: String,
    val password: String
)

data class RegisterRequestDto(
    val email: String,
    val password: String,
    val display_name: String = "",
    val invite_token: String? = null
)

data class VerifyEmailRequestDto(
    val email: String,
    val code: String
)

data class ResendVerificationRequestDto(
    val email: String
)

data class AccountDeletionRequestDto(
    val reason: String = ""
)

data class AccountDeletionResponseDto(
    val user_id: String,
    val email: String,
    val status: String,
    val requested_at: String? = null,
    val active_sessions_revoked: Int = 0,
    val message: String
)

data class AccountDeletionInfoDto(
    val app: String,
    val status: String,
    val authenticated_request_endpoint: String,
    val public_deletion_url_status: String,
    val retention_note: String
)

data class AuthResponseDto(
    val access_token: String,
    val token_type: String = "bearer",
    val user: AuthUserDto
)

data class RegistrationResponseDto(
    val user: AuthUserDto,
    val verification_required: Boolean = true,
    val message: String,
    val dev_verification_code: String? = null
)

data class VerifyEmailResponseDto(
    val user: AuthUserDto,
    val verified: Boolean = true,
    val message: String
)

data class RoleChoicesDto(
    val roles: List<String> = emptyList()
)

data class RoleUpdateRequestDto(
    val role: String
)

data class InviteCreateRequestDto(
    val email: String,
    val role: String = "REVIEWER",
    val expires_in_days: Int = 7
)

data class InviteDto(
    val id: String,
    val email: String,
    val role: String,
    val status: String,
    val expires_at: String? = null,
    val created_by_user_id: String,
    val accepted_by_user_id: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null
)

data class InviteCreateResponseDto(
    val id: String,
    val email: String,
    val role: String,
    val status: String,
    val expires_at: String? = null,
    val created_by_user_id: String,
    val accepted_by_user_id: String? = null,
    val created_at: String? = null,
    val updated_at: String? = null,
    val token: String
)

data class SessionDto(
    val id: String,
    val user_id: String,
    val user_email: String,
    val user_role: String,
    val created_at: String? = null,
    val expires_at: String? = null,
    val revoked: Boolean = false,
    val active: Boolean = false
)

data class SessionRevokeResponseDto(
    val target_user_id: String,
    val target_email: String,
    val revoked_sessions_count: Int
)
