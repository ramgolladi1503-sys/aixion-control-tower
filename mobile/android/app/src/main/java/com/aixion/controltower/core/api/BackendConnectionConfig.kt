package com.aixion.controltower.core.api

object BackendConnectionConfig {
    fun normalizeBaseUrl(rawBaseUrl: String): String {
        val trimmed = rawBaseUrl.trim()
        require(trimmed.isNotBlank()) { "AIXION_API_BASE_URL must not be blank." }
        return if (trimmed.endsWith("/")) trimmed else "$trimmed/"
    }

    fun healthUrl(rawBaseUrl: String): String {
        return "${normalizeBaseUrl(rawBaseUrl).trimEnd('/')}/health"
    }

    fun debugBuildCommand(rawBaseUrl: String): String {
        return "./gradlew assembleDebug -PAIXION_API_BASE_URL=${normalizeBaseUrl(rawBaseUrl)}"
    }
}
