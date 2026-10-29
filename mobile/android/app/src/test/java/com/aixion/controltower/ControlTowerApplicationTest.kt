package com.aixion.controltower

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ControlTowerApplicationTest {
    @Test
    fun signedOutOrMissingSessionStartupDoesNotQueryOrEnqueueRegistration() = runBlocking {
        var workStateChecked = false
        var enqueueCount = 0

        val result = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { false },
            hasActiveRegistrationWork = {
                workStateChecked = true
                false
            },
            enqueue = { enqueueCount += 1 },
            onFailure = { _, error -> throw AssertionError("Unexpected startup error", error) }
        )

        assertEquals(StartupPushReconciliation.SKIPPED_NO_SESSION, result)
        assertFalse(workStateChecked)
        assertEquals(0, enqueueCount)
    }

    @Test
    fun authenticatedStartupEnqueuesWhenNoRegistrationWorkIsActive() = runBlocking {
        var enqueueCount = 0

        val result = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { false },
            enqueue = { enqueueCount += 1 },
            onFailure = { _, error -> throw AssertionError("Unexpected startup error", error) }
        )

        assertEquals(StartupPushReconciliation.ENQUEUED, result)
        assertEquals(1, enqueueCount)
    }

    @Test
    fun authenticatedStartupDoesNotAddWorkWhenRegistrationIsAlreadyActive() = runBlocking {
        var enqueueCount = 0

        val result = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { true },
            enqueue = { enqueueCount += 1 },
            onFailure = { _, error -> throw AssertionError("Unexpected startup error", error) }
        )

        assertEquals(StartupPushReconciliation.SKIPPED_ACTIVE_WORK, result)
        assertEquals(0, enqueueCount)
    }

    @Test
    fun activeWorkQueryFailureIsReportedAndRecoversThroughUniqueEnqueue() = runBlocking {
        val queryError = IllegalStateException("work manager unavailable")
        val failures = mutableListOf<Pair<StartupPushFailureStage, Exception>>()
        var enqueueCount = 0

        val result = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { throw queryError },
            enqueue = { enqueueCount += 1 },
            onFailure = { stage, error -> failures += (stage to error) }
        )

        assertEquals(StartupPushReconciliation.ENQUEUED, result)
        assertEquals(1, enqueueCount)
        assertEquals(
            listOf(StartupPushFailureStage.ACTIVE_WORK_QUERY to queryError),
            failures
        )
    }

    @Test
    fun sessionLookupAndEnqueueFailuresAreReportedAndContained() = runBlocking {
        val sessionError = IllegalStateException("session unavailable")
        val enqueueError = IllegalStateException("enqueue unavailable")
        val failures = mutableListOf<Pair<StartupPushFailureStage, Exception>>()
        var enqueueCount = 0

        val sessionFailure = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { throw sessionError },
            hasActiveRegistrationWork = { false },
            enqueue = { enqueueCount += 1 },
            onFailure = { stage, error -> failures += (stage to error) }
        )
        val enqueueFailure = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { false },
            enqueue = { throw enqueueError },
            onFailure = { stage, error -> failures += (stage to error) }
        )

        assertEquals(StartupPushReconciliation.FAILED, sessionFailure)
        assertEquals(StartupPushReconciliation.FAILED, enqueueFailure)
        assertEquals(0, enqueueCount)
        assertEquals(
            listOf(
                StartupPushFailureStage.SESSION_LOOKUP to sessionError,
                StartupPushFailureStage.ENQUEUE to enqueueError
            ),
            failures
        )
    }

    @Test(expected = kotlinx.coroutines.CancellationException::class)
    fun startupReconciliationPropagatesCoroutineCancellation() = runBlocking {
        reconcileStartupPushRegistration(
            hasAuthenticatedSession = { throw kotlinx.coroutines.CancellationException() },
            hasActiveRegistrationWork = { false },
            enqueue = {},
            onFailure = { _, error ->
                throw AssertionError("Cancellation must not be swallowed", error)
            }
        )
    }
}
