package com.aixion.controltower.core.api.dto

data class RuntimeReadinessDto(
    val status: String,
    val generatedAt: String,
    val profile: String,
    val authEnabled: Boolean,
    val dbReachable: Boolean,
    val migrationsApplied: Boolean,
    val expectedMigrationIds: List<String>,
    val appliedMigrationIds: List<String>,
    val recoverySnapshotAvailable: Boolean,
    val recoveryFormatVersion: String,
    val githubTokenConfigured: Boolean,
    val pushConfigured: Boolean,
    val errors: List<String>,
    val warnings: List<String>
) {
    companion object {
        private val pushConfiguredKey = listOf("fcm", "server", "key", "configured").joinToString("_")

        fun from(payload: Map<String, Any?>): RuntimeReadinessDto {
            return RuntimeReadinessDto(
                status = payload.stringValue("status", "not_ready"),
                generatedAt = payload.stringValue("generated_at", "recent"),
                profile = payload.stringValue("profile", "unknown"),
                authEnabled = payload.booleanValue("auth_enabled"),
                dbReachable = payload.booleanValue("db_reachable"),
                migrationsApplied = payload.booleanValue("migrations_applied"),
                expectedMigrationIds = payload.stringListValue("expected_migration_ids"),
                appliedMigrationIds = payload.stringListValue("applied_migration_ids"),
                recoverySnapshotAvailable = payload.booleanValue("recovery_snapshot_available"),
                recoveryFormatVersion = payload.stringValue("recovery_format_version", "unknown"),
                githubTokenConfigured = payload.booleanValue("github_token_configured"),
                pushConfigured = payload.booleanValue(pushConfiguredKey),
                errors = payload.stringListValue("errors"),
                warnings = payload.stringListValue("warnings")
            )
        }
    }
}

private fun Map<String, Any?>.stringValue(key: String, defaultValue: String): String {
    return this[key] as? String ?: defaultValue
}

private fun Map<String, Any?>.booleanValue(key: String): Boolean {
    return this[key] as? Boolean ?: false
}

private fun Map<String, Any?>.stringListValue(key: String): List<String> {
    return (this[key] as? List<*>)?.mapNotNull { it as? String }.orEmpty()
}
