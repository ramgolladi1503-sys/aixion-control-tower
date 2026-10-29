package com.aixion.controltower.notifications

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PushDeviceRegistrationWorkerTest {
    @Test
    fun scheduledWorkCarriesNoTokenOrAccountInput() {
        val request = PushDeviceRegistrationWorker.newRequest()

        assertTrue(request.workSpec.input.keyValueMap.isEmpty())
    }

    @Test
    fun successfulOrPermissionDisabledRegistrationCompletes() {
        assertFalse(PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.REGISTERED))
        assertFalse(
            PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.PERMISSION_REQUIRED)
        )
    }

    @Test
    fun missingFirebaseBuildConfigurationDoesNotRetry() {
        assertFalse(
            PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.FIREBASE_NOT_CONFIGURED)
        )
    }

    @Test
    fun transientBackendAndCleanupFailuresRetry() {
        assertTrue(
            PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.BACKEND_UNAVAILABLE)
        )
        assertTrue(
            PushDeviceRegistrationWorker.shouldRetry(
                PushRegistrationResult.REGISTERED_CLEANUP_PENDING
            )
        )
        assertTrue(
            PushDeviceRegistrationWorker.shouldRetry(
                PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING
            )
        )
        assertFalse(
            PushDeviceRegistrationWorker.shouldRetry(
                PushRegistrationResult.AUTHENTICATION_REQUIRED
            )
        )
    }

    @Test
    fun transientRegistrationRetriesContinueAfterLongOutages() {
        for (attempt in listOf(0, 8, 100)) {
            assertTrue(
                "Transient failures must retry at attempt $attempt",
                PushDeviceRegistrationWorker.resultForRegistrationResult(
                    PushRegistrationResult.BACKEND_UNAVAILABLE,
                    attempt
                ) is androidx.work.ListenableWorker.Result.Retry
            )
        }
    }

    @Test
    fun authenticationFailureStopsBackgroundRetryUntilSessionRecovery() {
        assertTrue(
            PushDeviceRegistrationWorker.resultForRegistrationResult(
                PushRegistrationResult.AUTHENTICATION_REQUIRED,
                8
            ) is androidx.work.ListenableWorker.Result.Failure
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun negativeWorkManagerAttemptCountIsRejected() {
        PushDeviceRegistrationWorker.retryResultForAttempt(-1)
    }
}
