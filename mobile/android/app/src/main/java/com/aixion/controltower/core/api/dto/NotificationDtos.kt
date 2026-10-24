package com.aixion.controltower.core.api.dto

data class DeviceRegistrationRequestDto(
    val token: String,
    val platform: String = "android",
    val app_version: String = ""
)

data class DeviceRegistrationDto(
    val id: String,
    val user_id: String? = null,
    val workspace_id: String? = null,
    val platform: String = "android",
    val app_version: String = "",
    val created_at: String? = null,
    val updated_at: String? = null
)
