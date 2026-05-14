package com.aixion.controltower.core.model

data class RuntimeReadinessSummary(
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
    val isReady: Boolean
        get() = status.equals("ready", ignoreCase = true) &&
            dbReachable &&
            migrationsApplied &&
            recoverySnapshotAvailable &&
            errors.isEmpty()
}
