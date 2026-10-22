package com.aixion.controltower.notifications

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.aixion.controltower.core.auth.AuthSession
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Registers the latest FCM token using the currently saved authenticated API
 * session. The token and auth credential are deliberately not WorkManager input.
 */
internal class PushDeviceRegistrationWorker(
    appContext: Context,
    workerParameters: WorkerParameters
) : CoroutineWorker(appContext, workerParameters) {
    override suspend fun doWork(): Result {
        val accessToken = try {
            AuthSession.getAccessToken(applicationContext)
        } catch (_: Exception) {
            return retryOrStop()
        }
        if (accessToken.isBlank()) {
            // Leave the encrypted pending token untouched. Login/account resume
            // already invokes registration using Firebase's current token.
            return Result.success()
        }
        val registrationResult = try {
            PushDeviceRegistrationRepository(applicationContext).registerCurrentDevice()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            // WorkManager retries without exposing exception text, which may
            // contain request or account details from a lower-level client.
            return retryOrStop()
        }
        return if (shouldRetry(registrationResult)) retryOrStop() else Result.success()
    }

    private fun retryOrStop(): Result =
        if (shouldRetryWithinLimit(runAttemptCount)) Result.retry() else Result.failure()

    companion object {
        private const val UNIQUE_WORK_NAME = "aixion_push_device_registration"
        private const val BACKOFF_MILLIS = 30_000L
        private const val MAX_ATTEMPTS = 8
        private const val CANCEL_TIMEOUT_SECONDS = 10L

        fun enqueue(context: Context) {
            // Append after an active attempt instead of cancelling an in-flight
            // request. Each run reads Firebase's latest token; the job contains
            // no token or account identity and resolves auth at run time.
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(
                    UNIQUE_WORK_NAME,
                    ExistingWorkPolicy.APPEND_OR_REPLACE,
                    newRequest()
                )
        }

        internal fun newRequest() =
            OneTimeWorkRequestBuilder<PushDeviceRegistrationWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .setBackoffCriteria(
                    androidx.work.BackoffPolicy.EXPONENTIAL,
                    BACKOFF_MILLIS,
                    TimeUnit.MILLISECONDS
                )
                .build()

        suspend fun cancelAndAwait(context: Context) {
            withContext(Dispatchers.IO) {
                WorkManager.getInstance(context.applicationContext)
                    .cancelUniqueWork(UNIQUE_WORK_NAME)
                    .result
                    .get(CANCEL_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            }
        }

        internal fun shouldRetryWithinLimit(runAttemptCount: Int): Boolean =
            runAttemptCount < MAX_ATTEMPTS

        internal fun shouldRetry(result: PushRegistrationResult): Boolean = when (result) {
            PushRegistrationResult.REGISTERED,
            PushRegistrationResult.PERMISSION_REQUIRED -> false

            PushRegistrationResult.REGISTERED_CLEANUP_PENDING,
            PushRegistrationResult.PERMISSION_REQUIRED_CLEANUP_PENDING,
            PushRegistrationResult.FIREBASE_NOT_CONFIGURED,
            PushRegistrationResult.BACKEND_UNAVAILABLE -> true
        }
    }
}
