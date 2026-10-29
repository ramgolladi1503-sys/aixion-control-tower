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
            onFailure = { throw AssertionError("Unexpected startup error", it) }
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
            onFailure = { throw AssertionError("Unexpected startup error", it) }
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
            onFailure = { throw AssertionError("Unexpected startup error", it) }
        )

        assertEquals(StartupPushReconciliation.SKIPPED_ACTIVE_WORK, result)
        assertEquals(0, enqueueCount)
    }

    @Test
    fun startupReconciliationContainsSessionAndWorkManagerFailures() = runBlocking {
        var enqueueCount = 0
        val errors = mutableListOf<Exception>()

        val sessionFailure = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { throw IllegalStateException("session unavailable") },
            hasActiveRegistrationWork = { false },
            enqueue = { enqueueCount += 1 },
            onFailure = { errors.add(it) }
        )
        val workManagerFailure = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { throw IllegalStateException("work manager unavailable") },
            enqueue = { enqueueCount += 1 },
            onFailure = { errors.add(it) }
        )
        val enqueueFailure = reconcileStartupPushRegistration(
            hasAuthenticatedSession = { true },
            hasActiveRegistrationWork = { false },
            enqueue = { throw IllegalStateException("enqueue unavailable") },
            onFailure = { errors.add(it) }
        )

        assertEquals(StartupPushReconciliation.FAILED, sessionFailure)
        assertEquals(StartupPushReconciliation.FAILED, workManagerFailure)
        assertEquals(StartupPushReconciliation.FAILED, enqueueFailure)
        assertEquals(0, enqueueCount)
        assertEquals(3, errors.size)
    }

    @Test(expected = kotlinx.coroutines.CancellationException::class)
    fun startupReconciliationPropagatesCoroutineCancellation() = runBlocking {
        reconcileStartupPushRegistration(
            hasAuthenticatedSession = { throw kotlinx.coroutines.CancellationException() },
            hasActiveRegistrationWork = { false },
            enqueue = {},
            onFailure = { throw AssertionError("Cancellation must not be swallowed", it) }
        )
    }
}
