package com.aixion.controltower.data.repository

import com.aixion.controltower.notifications.PushRegistrationResult
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthRepositoryPushRegistrationTest {
    @Test
    fun transientPostLoginFailureQueuesDurableRetry() = runTest {
        var scheduled = 0

        val result = registerPushDeviceAndScheduleRetries(
            register = { PushRegistrationResult.BACKEND_UNAVAILABLE },
            scheduleRetry = { scheduled += 1 }
        )

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals(1, scheduled)
    }

    @Test
    fun successAndAuthenticationFailureDoNotQueueRetry() = runTest {
        for (registrationResult in listOf(
            PushRegistrationResult.REGISTERED,
            PushRegistrationResult.AUTHENTICATION_REQUIRED
        )) {
            var scheduled = 0
            val result = registerPushDeviceAndScheduleRetries(
                register = { registrationResult },
                scheduleRetry = { scheduled += 1 }
            )

            assertEquals(registrationResult, result)
            assertEquals(0, scheduled)
        }
    }

    @Test
    fun retrySchedulingFailureDoesNotReplaceRegistrationResult() = runTest {
        var reported = 0

        val result = registerPushDeviceAndScheduleRetries(
            register = { PushRegistrationResult.BACKEND_UNAVAILABLE },
            scheduleRetry = { throw IllegalStateException("scheduler unavailable") },
            onScheduleFailure = { reported += 1 }
        )

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals(1, reported)
    }

    @Test
    fun unexpectedRegistrationFailureBecomesRetryableAndQueuesWork() = runTest {
        var scheduled = 0
        var reported = 0

        val result = registerPushDeviceAndScheduleRetries(
            register = { throw IOException("temporary network failure") },
            scheduleRetry = { scheduled += 1 },
            onRegistrationFailure = { reported += 1 }
        )

        assertEquals(PushRegistrationResult.BACKEND_UNAVAILABLE, result)
        assertEquals(1, scheduled)
        assertEquals(1, reported)
    }

    @Test
    fun cancellationDuringRetrySchedulingPropagates() = runTest {
        val failure = runCatching {
            registerPushDeviceAndScheduleRetries(
                register = { PushRegistrationResult.BACKEND_UNAVAILABLE },
                scheduleRetry = { throw CancellationException("cancelled") }
            )
        }.exceptionOrNull()

        assertTrue(failure is CancellationException)
    }
}
