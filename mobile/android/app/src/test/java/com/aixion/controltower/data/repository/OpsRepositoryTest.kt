package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.OpsApi
import com.aixion.controltower.core.api.dto.RuntimeReadinessDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class OpsRepositoryTest {
    @Test
    fun getRuntimeReadinessMapsBackendReadinessToUiSummary() = runTest {
        val repository = OpsRepository(SuccessOpsApi())

        val readiness = repository.getRuntimeReadiness()

        assertTrue(readiness.isReady)
        assertEquals("Ready", readiness.readinessLabel)
        assertEquals("production", readiness.profile)
        assertTrue(readiness.authEnabled)
        assertTrue(readiness.dbReachable)
        assertTrue(readiness.migrationsApplied)
        assertTrue(readiness.recoverySnapshotAvailable)
        assertTrue(readiness.githubTokenConfigured)
        assertFalse(readiness.fcmServerKeyConfigured)
        assertEquals(listOf("0002_future"), readiness.missingMigrationIds)
        assertTrue(readiness.hasSecretWarnings)
    }

    @Test
    fun getRuntimeReadinessThrowsWhenBackendFetchFails() = runTest {
        val repository = OpsRepository(FailingOpsApi())

        try {
            repository.getRuntimeReadiness()
        } catch (error: IllegalStateException) {
            assertEquals("Unable to load runtime readiness", error.message)
            return@runTest
        }

        fail("Expected IllegalStateException to be thrown")
    }
}

private class SuccessOpsApi : OpsApi {
    override suspend fun getRuntimeReadiness(): RuntimeReadinessDto {
        return RuntimeReadinessDto(
            status = "ready",
            generated_at = "2026-05-14T00:00:00Z",
            profile = "production",
            auth_enabled = true,
            db_reachable = true,
            migrations_applied = true,
            expected_migration_ids = listOf("0001_baseline_kv_store", "0002_future"),
            applied_migration_ids = listOf("0001_baseline_kv_store"),
            recovery_snapshot_available = true,
            recovery_format_version = "aixion-control-tower-recovery-v1",
            github_token_configured = true,
            fcm_server_key_configured = false,
            errors = emptyList(),
            warnings = listOf("FCM server key is not configured")
        )
    }
}

private class FailingOpsApi : OpsApi {
    override suspend fun getRuntimeReadiness(): RuntimeReadinessDto {
        throw IllegalStateException("backend readiness failed")
    }
}
