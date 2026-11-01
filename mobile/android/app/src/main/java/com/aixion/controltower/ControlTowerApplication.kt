package com.aixion.controltower

import android.app.Application
import android.util.Log
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.aixion.controltower.core.auth.AuthSession
import com.aixion.controltower.notifications.ApprovalNotificationChannel
import com.aixion.controltower.notifications.PushDeviceRegistrationWorker
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

internal enum class StartupPushReconciliation {
    SKIPPED_NO_SESSION,
    SKIPPED_ACTIVE_WORK,
    ENQUEUED,
    FAILED
}

internal enum class StartupPushFailureStage {
    SESSION_LOOKUP,
    ACTIVE_WORK_QUERY,
    ENQUEUE
}

/**
 * Reconciles startup scheduling without putting account or token data into
 * WorkManager input. Failures are reported to the caller and do not escape into
 * Application startup.
 */
internal suspend fun reconcileStartupPushRegistration(
    hasAuthenticatedSession: suspend () -> Boolean,
    hasActiveRegistrationWork: suspend () -> Boolean,
    enqueue: () -> Unit,
    onFailure: (StartupPushFailureStage, Exception) -> Unit
): StartupPushReconciliation {
    val hasSession = try {
        hasAuthenticatedSession()
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (error: Exception) {
        onFailure(StartupPushFailureStage.SESSION_LOOKUP, error)
        return StartupPushReconciliation.FAILED
    }
    if (!hasSession) return StartupPushReconciliation.SKIPPED_NO_SESSION

    val hasActiveWork = try {
        hasActiveRegistrationWork()
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (error: Exception) {
        // A failed state query is not evidence that work is already present.
        // Report it, then use WorkManager's unique enqueue path to recover.
        onFailure(StartupPushFailureStage.ACTIVE_WORK_QUERY, error)
        false
    }
    if (hasActiveWork) return StartupPushReconciliation.SKIPPED_ACTIVE_WORK

    return try {
        enqueue()
        StartupPushReconciliation.ENQUEUED
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (error: Exception) {
        onFailure(StartupPushFailureStage.ENQUEUE, error)
        StartupPushReconciliation.FAILED
    }
}

class ControlTowerApplication : Application() {
    private val startupScope by lazy {
        CoroutineScope(SupervisorJob() + Dispatchers.IO)
    }

    override fun onCreate() {
        super.onCreate()
        ApprovalNotificationChannel.ensureCreated(this)
        val appContext = applicationContext
        startupScope.launch {
            reconcileStartupPushRegistration(
                hasAuthenticatedSession = {
                    AuthSession.getAccessToken(appContext).isNotBlank()
                },
                hasActiveRegistrationWork = {
                    WorkManager.getInstance(appContext)
                        .getWorkInfosByTag(PushDeviceRegistrationWorker::class.java.name)
                        .get()
                        .any { workInfo ->
                            workInfo.state == WorkInfo.State.ENQUEUED ||
                                workInfo.state == WorkInfo.State.BLOCKED ||
                                workInfo.state == WorkInfo.State.RUNNING
                        }
                },
                enqueue = { PushDeviceRegistrationWorker.enqueue(appContext) },
                onFailure = { stage, _ ->
                    // Deliberately omit exception details: lower-level failures
                    // can contain account, request, or infrastructure data.
                    Log.w(TAG, "Startup push registration $stage failed.")
                }
            )
        }
    }

    private companion object {
        const val TAG = "ControlTowerApplication"
    }
}
