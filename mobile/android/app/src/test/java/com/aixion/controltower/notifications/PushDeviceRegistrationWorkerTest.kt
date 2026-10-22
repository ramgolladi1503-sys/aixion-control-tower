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
    fun transientBackendFirebaseAndCleanupFailuresRetry() {
        assertTrue(
            PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.BACKEND_UNAVAILABLE)
        )
        assertTrue(
            PushDeviceRegistrationWorker.shouldRetry(PushRegistrationResult.FIREBASE_NOT_CONFIGURED)
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
    }

    @Test
    fun retriesStopAfterBoundedAttempts() {
        assertTrue(PushDeviceRegistrationWorker.shouldRetryWithinLimit(0))
        assertTrue(PushDeviceRegistrationWorker.shouldRetryWithinLimit(7))
        assertFalse(PushDeviceRegistrationWorker.shouldRetryWithinLimit(8))
    }
}
